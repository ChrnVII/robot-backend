"""Compiled SQL shares the existing psycopg transaction and pool."""

import os

import pytest
from sqlalchemy import bindparam, text

from app.robot_core.storage import PlatformStore
from app.robot_core.storage.runs import InspectionRunStore


def test_compiled_sql_preserves_literals_and_expanding_parameters():
    store = PlatformStore(os.environ["ROBOT_TEST_DATABASE_URL"])
    try:
        with store.connect() as connection:
            row = connection.execute_statement(
                text("SELECT '?' AS question, '100%' AS percent, CAST(:number AS integer) AS number WHERE 2 IN :items")
                .bindparams(bindparam("items", expanding=True)), {"number": 7, "items": [1, 2]},
            ).fetchone()
            assert row == {"question": "?", "percent": "100%", "number": 7}
    finally:
        store.close()


def test_checkpoint_event_outbox_transaction_rolls_back_together(monkeypatch):
    store = PlatformStore(os.environ["ROBOT_TEST_DATABASE_URL"])
    runs = InspectionRunStore(store)
    try:
        def fail_notify(*args):
            raise RuntimeError("failure after checkpoint, event and outbox writes")
        monkeypatch.setattr(store, "_notify", fail_notify)
        with pytest.raises(RuntimeError, match="failure after"):
            runs.save({"task_run_id": "atomic-run", "state": "running"})
        assert runs.status("atomic-run") is None
        with store.connect() as connection:
            assert connection.execute("SELECT count(*) AS count FROM events").fetchone()["count"] == 0
            assert connection.execute("SELECT count(*) AS count FROM outbox").fetchone()["count"] == 0
    finally:
        store.close()
