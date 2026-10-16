import pytest

from app.common.deployment import ConfigurationError, validate_backend_deployment, validate_deployment
from app.common.config import Settings

pytestmark = pytest.mark.no_database


@pytest.fixture
def values():
    return {"robot": {"robot_id": "robot-1"}, "runtime": {"mode": "simulated"},
            "storage": {"database_url": "postgresql://owner:secret@localhost/platform"}}


def test_role_credentials_may_differ(values):
    validate_deployment(values, {"ROBOT_CORE_DATABASE_URL": "postgresql+psycopg://core:other@127.0.0.1:5432/platform"})


@pytest.mark.parametrize("variable,value", [
    ("ROBOT_ROBOT_ID", "another"), ("ROBOT_MODE", "hardware"),
    ("ROBOT_DATABASE_URL", "postgresql://api:secret@localhost/another"),
    ("ROBOT_PERCEPTION_DATABASE_URL", "postgresql://api:secret@localhost:5433/platform"),
    ("ROBOT_SYNC_DATABASE_URL", "postgresql://api:secret@remote/platform"),
    ("ROBOT_MIGRATION_DATABASE_URL", "postgresql://api:secret@localhost/platform?host=remote"),
])
def test_conflicts_fail_without_leaking_credentials(values, variable, value):
    with pytest.raises(ConfigurationError) as error:
        validate_deployment(values, {variable: value})
    assert variable in str(error.value)
    assert "secret" not in str(error.value)


def test_unix_socket_database_targets(values):
    values["storage"]["database_url"] = "postgresql:///platform?host=/var/run/postgresql"
    validate_deployment(values, {"ROBOT_DATABASE_URL": "postgresql://api@/platform?host=/var/run/postgresql&port=5432"})


def test_backend_checks_effective_settings_including_dotenv(tmp_path, monkeypatch):
    import yaml
    config = tmp_path / "platform.yaml"
    config.write_text(yaml.safe_dump({"robot": {"robot_id": "robot-1"}, "runtime": {"mode": "simulated"},
                                    "storage": {"database_url": "postgresql://owner@localhost/platform"}}))
    dotenv = tmp_path / "robot.env"
    dotenv.write_text("ROBOT_ROBOT_ID=wrong\n")
    monkeypatch.delenv("ROBOT_ROBOT_ID", raising=False)
    settings = Settings(_env_file=dotenv, platform_config=str(config), mode="simulated", database_url="postgresql://api@localhost/platform")
    with pytest.raises(ConfigurationError, match="ROBOT_ROBOT_ID"):
        validate_backend_deployment(settings)
