"""配置推导测试。

`--server` 允许写 https 也可以写 wss，两种都必须能正确推出 WS 地址和 HTTP base。
写错这里不会报错，只会表现为"能连上但上传 404"——所以必须测。
"""

from __future__ import annotations

import pytest

from app.cloud_agent.config import AgentConfig, ConfigError, load_config

pytestmark = pytest.mark.no_database


@pytest.mark.parametrize(
    ("server", "ws_url", "http_base"),
    [
        ("https://files.example.com", "wss://files.example.com/ws/agent", "https://files.example.com"),
        ("https://files.example.com/", "wss://files.example.com/ws/agent", "https://files.example.com"),
        ("wss://files.example.com/ws/agent", "wss://files.example.com/ws/agent", "https://files.example.com"),
        ("ws://127.0.0.1:8000", "ws://127.0.0.1:8000/ws/agent", "http://127.0.0.1:8000"),
        ("http://127.0.0.1:8000/", "ws://127.0.0.1:8000/ws/agent", "http://127.0.0.1:8000"),
        ("https://example.com/fb", "wss://example.com/fb/ws/agent", "https://example.com/fb"),
        ("wss://example.com/fb/ws/agent", "wss://example.com/fb/ws/agent", "https://example.com/fb"),
    ],
)
def test_url_derivation(server, ws_url, http_base):
    config = AgentConfig(server=server, token="t", root="/tmp")

    assert config.ws_url == ws_url
    assert config.http_base == http_base


def test_bad_scheme_is_reported():
    config = AgentConfig(server="ftp://host", token="t", root="/tmp")
    with pytest.raises(ConfigError):
        _ = config.http_base


def test_validate_does_not_require_credentials():
    """server / token / root 由 ensure_credentials 补（它能读本地凭据文件）。

    所以 validate() 不该因为"参数没给"就拒绝——否则"只填一次 IP、之后无参数启动"
    这条路走不通。
    """
    AgentConfig().validate()


def test_validate_still_rejects_a_bad_scheme():
    with pytest.raises(ConfigError):
        AgentConfig(server="ftp://host").validate()


def test_concurrency_must_be_positive():
    with pytest.raises(ConfigError):
        AgentConfig(server="https://x", token="t", root="/tmp", max_concurrent_uploads=0).validate()


def test_load_from_toml_and_env(tmp_path, monkeypatch):
    config_file = tmp_path / "agent.toml"
    config_file.write_text(
        'server = "https://from-file.example.com"\nroot = "/srv/share"\n'
        'token = "from-file"\nmax_concurrent_uploads = 2\n',
        encoding="utf-8",
    )

    config = load_config(str(config_file))
    assert config.server.endswith("from-file.example.com")
    assert config.max_concurrent_uploads == 2

    monkeypatch.setenv("ROBOT_CLOUD_AGENT_TOKEN", "from-env")
    monkeypatch.setenv("ROBOT_CLOUD_AGENT_HEARTBEAT_INTERVAL", "5")
    config = load_config(str(config_file))
    assert config.token == "from-env"
    assert config.heartbeat_interval == 5.0

    config = load_config(str(config_file), token="from-cli")
    assert config.token == "from-cli"


def test_unknown_key_in_file_is_rejected(tmp_path):
    config_file = tmp_path / "bad.toml"
    config_file.write_text('server = "https://x"\nunknown_key = 1\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="未知的配置项"):
        load_config(str(config_file))


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(str(tmp_path / "nope.toml"))


def test_shared_socket_env_name_is_honored(monkeypatch):
    """backend 用 ROBOT_CLOUD_AGENT_SOCKET；本进程按字段推导是 ..._CONTROL_SOCKET。

    部署时只改一处也必须让两端连同一个 socket，否则页面永远是「不可达」。
    """
    monkeypatch.setenv("ROBOT_CLOUD_AGENT_SOCKET", "/tmp/shared.sock")
    assert load_config().control_socket == "/tmp/shared.sock"

    monkeypatch.setenv("ROBOT_CLOUD_AGENT_CONTROL_SOCKET", "/tmp/specific.sock")
    assert load_config().control_socket == "/tmp/specific.sock"
