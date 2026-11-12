"""入口的平台门禁。

"只支持 Linux"必须是可执行的一条断言，不能只是文档里的一句话——文档会被跳过，
断言不会。

注意：这里测的是 `require_linux()` 本身，不是去真的启动进程。单元测试与端到端
测试都不经过 CLI 入口，所以它们在任何开发机上都能跑，和这条门禁不冲突。
"""

from __future__ import annotations

import pytest

from app.cloud_agent.main import require_linux

pytestmark = pytest.mark.no_database


def test_linux_is_accepted():
    assert require_linux("linux") is None


@pytest.mark.parametrize("platform_name", ["win32", "darwin", "freebsd", "cygwin"])
def test_other_platforms_exit_with_message(platform_name, capsys):
    with pytest.raises(SystemExit) as excinfo:
        require_linux(platform_name)

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "只支持 Linux" in stderr
    assert platform_name in stderr


def test_entrypoint_calls_the_guard(monkeypatch):
    """main() 必须真的调用门禁，否则这条断言就只是摆设。"""
    import sys

    from app.cloud_agent import main as main_module

    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(SystemExit) as excinfo:
        main_module.main(["--server", "https://x", "--token", "t", "--root", "/tmp"])

    assert excinfo.value.code == 2
