"""One migration history shared by the API, control and perception processes."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.common.dburl import require_postgres_url

HEAD_REVISION = "0002_perception"


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
