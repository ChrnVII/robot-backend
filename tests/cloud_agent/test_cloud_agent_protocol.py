"""协议契约测试（B 端）。

与 server 那份是同一套检查，只是路径与导入不同：两端都读仓库根的
`contract/protocol-frames.json`，而且要求两份 `protocol.py` 逐字节一致。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.cloud_agent.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    CommandName,
    ErrorCode,
    Message,
    MessageType,
    ProtocolError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "tests" / "contracts" / "protocol-frames.json"
OWN_COPY = REPO_ROOT / "app" / "cloud_agent" / "protocol.py"
# server 在另一个仓库。本地开发目录里通常是兄弟目录；CI 用环境变量指过去，
# 指不到就跳过源码比对（黄金样例仍会跑）。
SERVER_COPY = Path(
    os.environ.get(
        "ROBOT_MONITOR_PROTOCOL",
        REPO_ROOT.parent.parent / "robot-monitor" / "server" / "backend" / "src"
        / "robot_monitor_server" / "protocol.py",
    )
)

pytestmark = pytest.mark.no_database


if not CONTRACT_PATH.is_file():
    pytest.skip(f"契约样例不存在：{CONTRACT_PATH}（只部署一端时会跳过）", allow_module_level=True)

CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
FRAMES = CONTRACT["frames"]
FRAME_IDS = [case["name"] for case in FRAMES]


def test_protocol_copy_is_identical():
    if not SERVER_COPY.is_file():
        pytest.skip("server 不在同级目录（独立部署场景），跳过源码比对")

    assert OWN_COPY.read_bytes() == SERVER_COPY.read_bytes(), (
        "agent 与 server 的 protocol.py 已经不一致。改协议时两边要一起改，"
        "或者先把 contract/protocol-frames.json 改对再同步两份实现。"
    )


def test_contract_version_matches_code():
    assert CONTRACT["protocol_version"] == PROTOCOL_VERSION
    assert CONTRACT["max_frame_bytes"] == MAX_FRAME_BYTES


@pytest.mark.parametrize("case", FRAMES, ids=FRAME_IDS)
def test_golden_frame_roundtrip(case):
    raw = json.dumps(case["frame"], ensure_ascii=False)
    message = Message.parse(raw)

    assert message.v == PROTOCOL_VERSION
    assert message.type.value == case["frame"]["type"]
    assert message.id == case["frame"]["id"]
    assert json.loads(message.to_json()) == case["frame"]


@pytest.mark.parametrize("case", FRAMES, ids=FRAME_IDS)
def test_golden_command_names_are_known(case):
    command = case["frame"].get("command")
    if command is not None:
        assert command in {item.value for item in CommandName}


@pytest.mark.parametrize("case", CONTRACT["rejected"], ids=[c["name"] for c in CONTRACT["rejected"]])
def test_rejected_frames(case):
    with pytest.raises(ProtocolError, match=case["error_contains"]):
        Message.parse(json.dumps(case["frame"]))


def test_unknown_command_parses_but_is_not_dispatchable():
    raw = json.dumps({"v": 1, "type": "command", "id": "x", "command": "run_command"})
    message = Message.parse(raw)

    assert message.command == "run_command"
    assert message.command_name is None


def test_command_helpers_build_expected_shapes():
    command = Message.command_frame(CommandName.PING, {"echo": 1}, id="fixed")
    body = json.loads(command.to_json())
    assert body["command"] == "ping"
    assert body["payload"] == {"echo": 1}

    result = Message.result_frame("fixed", {"entries": []})
    assert json.loads(result.to_json())["ok"] is True

    error = Message.error_frame("fixed", ErrorCode.FORBIDDEN, "nope")
    assert json.loads(error.to_json())["error"]["code"] == "forbidden"


def test_oversized_frame_is_rejected():
    huge = {"v": 1, "type": "command", "id": "x", "payload": {"blob": "a" * MAX_FRAME_BYTES}}
    with pytest.raises(ProtocolError, match="too large"):
        Message.parse(json.dumps(huge).encode())


def test_non_utf8_bytes_are_rejected():
    with pytest.raises(ProtocolError, match="utf-8"):
        Message.parse(b"\xff\xfe\x00")


def test_malformed_json_is_rejected():
    with pytest.raises(ProtocolError, match="malformed json"):
        Message.parse("{not json")


def test_message_type_enum_matches_contract():
    used = {case["frame"]["type"] for case in FRAMES}
    assert used <= {item.value for item in MessageType}
