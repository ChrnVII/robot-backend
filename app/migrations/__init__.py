"""One migration history shared by the API, control and perception processes."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.common.dburl import require_postgres_url

#: 必须与 app/migrations/versions 里最新的 revision 保持一致。
#: 这个常量是「生产不开自动迁移」时的唯一校验依据：写成旧值会让 check_database()
#: 在库已经升到新版本时反过来判定版本不匹配，直接拒绝启动。
#: 新增迁移时同步改这里，否则 ROBOT_AUTO_MIGRATE=false 的部署起不来。
HEAD_REVISION = "0004_alert_type"


def check_database(target: Engine | str) -> None:
    """Validate the deployed revision without DDL or data writes."""
    engine = target if isinstance(target, Engine) else create_engine(require_postgres_url(target, "database_url"))
    try:
        with engine.connect() as connection:
            revisions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
        if revisions != {HEAD_REVISION}:
            raise RuntimeError("database revision mismatch; run robot-db upgrade with the migration role")
    finally:
        if not isinstance(target, Engine):
            engine.dispose()


def upgrade_database(target: Engine | str) -> None:
    engine = target if isinstance(target, Engine) else create_engine(require_postgres_url(target, "database_url"))
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('robot_platform_alembic'))"))
            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parent))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        if not isinstance(target, Engine):
            engine.dispose()
