from alembic import context
from sqlalchemy import create_engine, pool

from app.common.config import get_settings
from app.common.dburl import require_postgres_url


def run(connection):
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    engine = create_engine(require_postgres_url(get_settings().database_url, "ROBOT_DATABASE_URL"), poolclass=pool.NullPool)
    with engine.begin() as connection:
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('robot_platform_alembic'))")
        run(connection)
    engine.dispose()
