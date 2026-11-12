"""指令处理器测试：分页、排序、失败码。

分页是这个模块最容易出错的地方：`next_cursor` 差一位就会漏文件或重复文件，
而列表看起来"差不多对"，很容易漏测。
"""

from __future__ import annotations

import pytest

from app.cloud_agent.errors import AgentCommandError
from app.cloud_agent.handlers import HandlerContext, dispatch
from app.cloud_agent.protocol import CommandName, ErrorCode
from app.cloud_agent.sandbox import Sandbox, SandboxError

pytestmark = pytest.mark.no_database


class DummyUploads:
    async def start(self, job):  # pragma: no cover - 只用于类型占位
        self.job = job

    async def cancel(self, upload_id):  # pragma: no cover
        return None


@pytest.fixture()
def ctx(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "b.txt").write_text("b", encoding="utf-8")
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "报表 2026.xlsx").write_text("x", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("inner", encoding="utf-8")
    return HandlerContext(sandbox=Sandbox(root), uploads=DummyUploads())


async def test_list_dir_sorts_directories_first(ctx):
    result = await dispatch(CommandName.LIST_DIR, {"rel": ""}, ctx)

    types = [entry["type"] for entry in result["entries"]]
    assert types[0] == "dir"
    assert result["entries"][0]["name"] == "sub"
    assert result["total"] == 4


async def test_list_dir_pagination_covers_every_entry_exactly_once(ctx):
    seen: list[str] = []
    cursor = None

    for _ in range(10):
        payload = {"rel": "", "limit": 2}
        if cursor:
            payload["cursor"] = cursor
        page = await dispatch(CommandName.LIST_DIR, payload, ctx)
        seen.extend(entry["name"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == sorted(["sub", "a.txt", "b.txt", "报表 2026.xlsx"])
    assert len(seen) == len(set(seen))


async def test_list_dir_rel_paths_are_joined_with_slash(ctx):
    page = await dispatch(CommandName.LIST_DIR, {"rel": "sub"}, ctx)

    assert page["entries"][0]["rel"] == "sub/inner.txt"


async def test_list_dir_rejects_traversal(ctx):
    with pytest.raises(Exception) as excinfo:
        await dispatch(CommandName.LIST_DIR, {"rel": "../"}, ctx)

    assert "traversal" in str(excinfo.value)


async def test_list_dir_rejects_file_as_directory(ctx):
    with pytest.raises(AgentCommandError) as excinfo:
        await dispatch(CommandName.LIST_DIR, {"rel": "a.txt"}, ctx)

    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENTS


async def test_stat_file_returns_size_and_name(ctx):
    result = await dispatch(CommandName.STAT_FILE, {"rel": "a.txt"}, ctx)

    assert result["is_dir"] is False
    assert result["size"] == 1
    assert result["name"] == "a.txt"


async def test_stat_file_on_directory_is_flagged(ctx):
    result = await dispatch(CommandName.STAT_FILE, {"rel": "sub"}, ctx)

    assert result["is_dir"] is True


async def test_stat_file_missing_returns_file_not_found(ctx):
    with pytest.raises(AgentCommandError) as excinfo:
        await dispatch(CommandName.STAT_FILE, {"rel": "nope.txt"}, ctx)

    assert excinfo.value.code is ErrorCode.FILE_NOT_FOUND


async def test_stat_file_requires_a_path(ctx):
    with pytest.raises(AgentCommandError) as excinfo:
        await dispatch(CommandName.STAT_FILE, {"rel": ""}, ctx)

    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENTS


async def test_ping_echoes(ctx):
    result = await dispatch(CommandName.PING, {"echo": "hi"}, ctx)
    assert result["echo"] == "hi"
    assert result["pong"] > 0


async def test_start_upload_rejects_size_mismatch(ctx):
    payload = {
        "upload_id": "abc123",
        "rel": "a.txt",
        "total_size": 999,
        "chunk_size": 1024,
        "total_chunks": 1,
    }

    with pytest.raises(AgentCommandError) as excinfo:
        await dispatch(CommandName.START_UPLOAD, payload, ctx)

    assert excinfo.value.code is ErrorCode.SIZE_CHANGED


async def test_start_upload_rejects_inconsistent_chunk_count(ctx):
    payload = {
        "upload_id": "abc123",
        "rel": "a.txt",
        "total_size": 1,
        "chunk_size": 1024,
        "total_chunks": 5,
    }

    with pytest.raises(AgentCommandError) as excinfo:
        await dispatch(CommandName.START_UPLOAD, payload, ctx)

    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENTS


async def test_handler_table_matches_closed_command_enum():
    """命令枚举扩容但忘记写 handler 时，这条测试要红。"""
    from app.cloud_agent.handlers import _HANDLERS
    from app.cloud_agent.protocol import AGENT_ALLOWED_COMMANDS

    assert set(_HANDLERS) == set(AGENT_ALLOWED_COMMANDS)


async def test_no_write_mode_open_in_agent_source():
    """架构测试：agent 全代码库不得以写模式打开共享目录里的文件。

    B 是只读源。这条不变量靠 code review 守不住，靠扫描源码才守得住。
    """
    import pathlib
    import re

    source_dir = pathlib.Path(__file__).resolve().parents[2] / "app" / "cloud_agent"
    write_mode = re.compile(r"""open\([^)]*,\s*["'](?:w|a|r\+|wb|ab|x)""")

    # 唯一的例外：credentials.py 写凭据文件，config.py 写回配置文件。
    # 它们写的是"自己的状态"，不是共享目录；例外名单写死在这里，多出第二个文件就红。
    allowed = {"credentials.py", "config.py"}

    offenders = []
    for path in source_dir.glob("*.py"):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for match in write_mode.finditer(text):
            offenders.append(f"{path.name}: {match.group(0)}")

    assert offenders == [], f"agent 里出现了写模式打开文件：{offenders}"


async def test_hash_file_returns_real_digest(ctx):
    """判重靠这条命令：A 拿它算出的 sha256 和库里比对。"""
    import hashlib

    result = await dispatch(CommandName.HASH_FILE, {"rel": "a.txt"}, ctx)

    assert result["sha256"] == hashlib.sha256(b"a").hexdigest()
    assert result["size"] == 1


async def test_hash_file_reports_missing_file(ctx):
    with pytest.raises(AgentCommandError) as failure:
        await dispatch(CommandName.HASH_FILE, {"rel": "nope.txt"}, ctx)

    assert failure.value.code is ErrorCode.FILE_NOT_FOUND


async def test_hash_file_refuses_path_outside_sandbox(ctx):
    # 沙箱错误在这一层直接抛，由 client 的 dispatch 循环翻译成 forbidden
    with pytest.raises(SandboxError):
        await dispatch(CommandName.HASH_FILE, {"rel": "../etc/passwd"}, ctx)
