import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.robot_core.storage import PlatformStore
from app.robot_core.storage.seed import (
    COOP_SPECS,
    format_coop_code,
    format_point_agv,
    format_pointlayer_code,
    seed_prototype,
)


class InspectionDomainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PlatformStore(Path(self.temp.name) / "domain.db")
        seed_prototype(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_coop_specs_match_site(self):
        with self.store.connect() as connection:
            for spec in COOP_SPECS:
                row = connection.execute(
                    "SELECT row_count, column_count, layer_count FROM coops WHERE coop_no=?",
                    (spec["coop_no"],),
                ).fetchone()
                self.assertEqual(
                    tuple(row),
                    (spec["row_count"], spec["column_count"], spec["layer_count"]),
                )

    def test_agv_point_encoding_and_counts(self):
        with self.store.connect() as connection:
            total = connection.execute("SELECT count(*) FROM agv_points").fetchone()[0]
            self.assertEqual(total, 132 + 160)
            first_coop_last = connection.execute(
                "SELECT point_agv FROM agv_points WHERE coop_no=1 AND column_no=132"
            ).fetchone()[0]
            second_coop_last = connection.execute(
                "SELECT point_agv FROM agv_points WHERE coop_no=2 AND column_no=160"
            ).fetchone()[0]
            self.assertEqual(first_coop_last, "1132")
            self.assertEqual(second_coop_last, "2160")

    def test_cage_inventory_is_complete(self):
        expected = sum(spec["row_count"] * spec["column_count"] * spec["layer_count"] for spec in COOP_SPECS)
        with self.store.connect() as connection:
            total = connection.execute("SELECT count(*) FROM cages").fetchone()[0]
            self.assertEqual(total, expected)
            row = connection.execute(
                "SELECT coop_code, pointlayer_code FROM cages WHERE coop_no=1 AND row_no=2 AND column_no=2 AND layer_no=1"
            ).fetchone()
            self.assertEqual(row["coop_code"], "1-2-002")
            self.assertEqual(row["pointlayer_code"], "1-2-002-1002-1")

    def test_point_agv_is_unique(self):
        with self.store.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO agv_points(point_id, coop_id, coop_no, column_no, point_agv, enabled, created_at, updated_at)"
                    " VALUES ('point-dup', 'coop-1', 1, 999, '1001', 1, 'x', 'x')"
                )
            connection.rollback()

    def test_pointlayer_code_is_unique(self):
        with self.store.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO cages(cage_id, coop_id, point_id, coop_no, row_no, column_no, layer_no, coop_code, pointlayer_code, enabled, created_at)"
                    " VALUES ('cage-dup', 'coop-1', 'point-1002', 1, 1, 2, 1, '1-1-002', '1-2-002-1002-1', 1, 'x')"
                )
            connection.rollback()

    def test_code_formats(self):
        self.assertEqual(format_coop_code(1, 2, 2), "1-2-002")
        self.assertEqual(format_point_agv(1, 132), "1132")
        self.assertEqual(format_point_agv(2, 160), "2160")
        self.assertEqual(format_pointlayer_code("1-2-002", "1002", 1), "1-2-002-1002-1")

    def test_sample_inspection_record_is_aligned(self):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT coop_no, point_agv, layer_no, cage_id FROM inspection_records WHERE record_id='record-legacy-001'"
            ).fetchone()
            self.assertEqual(tuple(row), (1, "1002", 1, "cage-1002-2-1"))


if __name__ == "__main__":
    unittest.main()