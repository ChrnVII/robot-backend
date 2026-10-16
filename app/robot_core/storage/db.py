"""PostgreSQL connectivity shared by the robot runtime.

The platform is PostgreSQL-only: DSNs come from ``configs/*.yaml``
(``storage.database_url``) or the ``ROBOT_DATABASE_URL`` environment family.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Union


DatabaseTarget = Union[str, Path]
_QMARK = re.compile(r"\?")
_NAMED_PARAMETER = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def is_postgres_target(target: DatabaseTarget) -> bool:
    value = str(target).lower()
    return value.startswith(("postgresql://", "postgres://", "postgresql+psycopg://"))


def _postgres_dsn(target: DatabaseTarget) -> str:
    value = str(target)
    if value.startswith("postgresql+psycopg://"):
        return "postgresql://" + value.split("://", 1)[1]
    if value.startswith("postgres://"):
        return "postgresql://" + value.split("://", 1)[1]
    return value


def _qmark_to_pyformat(sql: str) -> str:
    """Translate the DB-API qmark style used by the repositories."""
    # psycopg treats percent signs as formatting markers. Escape literal
    # percent signs first so SQL LIKE patterns remain valid after translation.
    sql = sql.replace("%", "%%")
    if "?" in sql:
        return _QMARK.sub("%s", sql)
    # Mapping helpers use ``:field`` parameters; psycopg expects ``%(field)s``.
    return _NAMED_PARAMETER.sub(r"%(\1)s", sql)


class PostgresConnection:
    """Small compatibility wrapper while repositories are migrated to SQLAlchemy."""

    is_postgres = True

    def __init__(self, connection: Any, release: Optional[Callable[[Any], None]] = None):
        self._connection = connection
        self._release = release
        self._released = False

    def execute(self, sql: str, params: Optional[Union[Iterable[Any], Mapping[str, Any]]] = None):
        translated = _qmark_to_pyformat(sql)
        if isinstance(params, Mapping):
            return self._connection.execute(translated, params)
        return self._connection.execute(translated, tuple(params or ()))

    def executemany(self, sql: str, params: Iterable[Union[Iterable[Any], Mapping[str, Any]]]):
        translated = _qmark_to_pyformat(sql)
        items = list(params)
        values = items if (items and isinstance(items[0], Mapping)) else [tuple(item) for item in items]
        cursor = self._connection.cursor()
        cursor.executemany(translated, values)
        return cursor

    def executescript(self, script: str) -> None:
        # The schema contains ordinary DDL statements only; comments and blank
        # statements are ignored before executing each statement independently.
        statements = []
        for statement in script.split(";"):
            cleaned = "\n".join(
                line for line in statement.splitlines()
                if not line.strip().startswith("--")
            ).strip()
            if cleaned:
                statements.append(cleaned)
        for statement in statements:
            self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def __enter__(self) -> "PostgresConnection":
        try:
            self._connection.__enter__()
            return self
        except Exception:
            self._release_connection()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return_value = self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self._release_connection()
        return return_value

    def close(self) -> None:
        self._release_connection()

    def _release_connection(self) -> None:
        if self._released:
            return
        self._released = True
        release = self._release
        if release is None:
            self._connection.close()
        else:
            release(self._connection)


class PostgresConnectionPool:
    """Small lifecycle wrapper around psycopg-pool.

    The repositories use short-lived ``with store.connect()`` scopes.  A pool
    makes those scopes cheap while still committing or rolling back every
    transaction before the connection is returned to PostgreSQL.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8, timeout: float = 10.0):
        if min_size < 1 or max_size < min_size:
            raise ValueError("PostgreSQL pool sizes must satisfy 1 <= min_size <= max_size")
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise RuntimeError(
                "PostgreSQL pooling requires psycopg and psycopg_pool; install "
                "the project dependencies or run `pip install 'psycopg[binary]' psycopg-pool`."
            ) from exc
        self._pool = ConnectionPool(
            conninfo=_postgres_dsn(dsn),
            kwargs={"row_factory": dict_row, "connect_timeout": timeout},
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
        )
        self._timeout = timeout
        # Do not block process startup while PostgreSQL is offline. The first
        # business operation obtains a connection with a bounded timeout.
        self._pool.open(wait=False)

    def connection(self) -> PostgresConnection:
        connection = self._pool.getconn(timeout=self._timeout)
        return PostgresConnection(connection, release=self._pool.putconn)

    def close(self) -> None:
        self._pool.close()


def is_integrity_error(error: BaseException) -> bool:
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a required dependency
        return False
    return isinstance(error, psycopg.IntegrityError)
