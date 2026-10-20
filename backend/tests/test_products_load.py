import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from ingest import load_products, migrate_products, products


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        db = self.connection
        db.calls.append((sql, params))
        if db.fail_on and db.fail_on in sql:
            raise OSError("fake database failure")
        if sql.startswith("INSERT INTO products"):
            if db.pending is None:
                db.pending = copy.deepcopy(db.storage)
            db.pending[params[0]] = dict(zip(products.FIELDS, params))
        elif sql.startswith("DELETE FROM products"):
            db.pending = {key: value for key, value in db.pending.items() if key in params}

    def fetchone(self):
        return (self.connection.lock_result,)


class FakeConnection:
    def __init__(self):
        self.storage = {"existing-extra": {"sku_id": "existing-extra"}}
        self.pending = None
        self.calls = []
        self.commits = self.rollbacks = 0
        self.closed = False
        self.lock_result = 1
        self.fail_on = None

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        if self.pending is not None:
            self.storage, self.pending = self.pending, None

    def rollback(self):
        self.rollbacks += 1
        self.pending = None

    def close(self):
        self.closed = True


class ProductsLoadTests(unittest.TestCase):
    def setUp(self):
        self.rows = products.read_products()
        self.db = FakeConnection()
        self.path = str(products.ROOT / "data/products/products.csv")

    def test_dry_run_does_not_import_driver_or_create_connection(self):
        with patch.object(load_products.importlib, "import_module", side_effect=AssertionError("no imports")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(load_products.main([self.path, "--dry-run"],
                connection_factory=lambda: self.fail("dry-run must not connect")), 0)
        self.assertIn("50 款", output.getvalue())
        self.assertIn("未连接数据库", output.getvalue())
        self.assertIn("保留表中其他 SKU", output.getvalue())

    def test_dry_run_prune_is_explicit_and_does_not_connect(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(load_products.main([self.path, "--dry-run", "--prune", "--yes"],
                connection_factory=lambda: self.fail("no connection")), 0)
        self.assertIn("计划删除", output.getvalue())

    def test_prune_requires_both_flags_in_cli_and_function(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            load_products.main([self.path, "--prune", "--dry-run"])
        with self.assertRaisesRegex(ValueError, "--prune --yes"):
            load_products.upsert_products(self.db, self.rows, prune=True)
        self.assertEqual(self.db.calls, [])
        with self.assertRaisesRegex(ValueError, "空商品表"):
            load_products.upsert_products(self.db, [], prune=True, yes=True)

    def test_schema_is_idempotent_and_has_exact_csv_columns_with_json_specs(self):
        load_products.ensure_products_schema(self.db)
        load_products.ensure_products_schema(self.db)
        ddl = self.db.calls[0][0]
        self.assertEqual(ddl, self.db.calls[1][0])
        self.assertIn("CREATE TABLE IF NOT EXISTS products", ddl)
        self.assertIn("specs JSON NOT NULL", ddl)
        self.assertIn("PRIMARY KEY", ddl)
        self.assertEqual(self.db.commits, 2)
        for field in products.FIELDS:
            self.assertIn(field + " ", ddl)

    def test_upsert_twice_updates_values_without_duplicates_or_deleting_extras(self):
        self.assertEqual(load_products.upsert_products(self.db, self.rows), 50)
        snapshot = copy.deepcopy(self.db.storage)
        self.assertEqual(load_products.upsert_products(self.db, self.rows), 50)
        self.assertEqual(self.db.storage, snapshot)
        self.assertEqual(len(self.db.storage), 51)
        changed = copy.deepcopy(self.rows)
        changed[0]["tagline"] = "更新定位"
        load_products.upsert_products(self.db, changed)
        self.assertEqual(len(self.db.storage), 51)
        self.assertEqual(self.db.storage["tinglan-s1"]["tagline"], "更新定位")
        self.assertEqual(json.loads(self.db.storage["tinglan-s1"]["specs"]), self.rows[0]["specs"])
        self.assertIn("ON DUPLICATE KEY UPDATE", load_products.UPSERT)
        self.assertFalse(any(sql.startswith("DELETE") for sql, _ in self.db.calls))

    def test_prune_deletes_only_skus_missing_from_csv_after_upsert(self):
        load_products.upsert_products(self.db, self.rows, prune=True, yes=True)
        self.assertEqual(set(self.db.storage), {row["sku_id"] for row in self.rows})
        delete, = [(sql, params) for sql, params in self.db.calls if sql.startswith("DELETE")]
        self.assertEqual(delete[1], tuple(row["sku_id"] for row in self.rows))
        self.assertEqual(delete[0].count("%s"), 50)

    def test_sql_failure_rolls_back_and_releases_lock(self):
        self.db.fail_on = "DELETE FROM"
        with self.assertRaises(OSError):
            load_products.upsert_products(self.db, self.rows, prune=True, yes=True)
        self.assertEqual(set(self.db.storage), {"existing-extra"})
        self.assertEqual(self.db.rollbacks, 1)
        self.assertEqual(self.db.commits, 0)
        self.assertIn("RELEASE_LOCK", self.db.calls[-1][0])

    def test_lock_failure_does_not_mutate_or_release_unheld_lock(self):
        self.db.lock_result = 0
        with self.assertRaisesRegex(RuntimeError, "导入锁"):
            load_products.upsert_products(self.db, self.rows)
        self.assertEqual(len(self.db.calls), 1)
        self.assertEqual(self.db.rollbacks, 1)

    def test_cli_creates_schema_imports_then_closes_injected_connection(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(load_products.main([self.path], connection_factory=lambda: self.db), 0)
        self.assertTrue(self.db.closed)
        self.assertEqual(len(self.db.storage), 51)
        self.assertIn("50 款", output.getvalue())

    def test_cli_and_migration_close_connection_on_schema_failure(self):
        for entrypoint, argv in ((load_products.main, [self.path]), (migrate_products.main, [])):
            db = FakeConnection()
            db.fail_on = "CREATE TABLE"
            with self.subTest(entrypoint=entrypoint), redirect_stderr(io.StringIO()):
                self.assertEqual(entrypoint(argv, connection_factory=lambda: db), 1)
            self.assertTrue(db.closed)

    def test_migration_uses_same_schema_and_injected_connection(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(migrate_products.main([], connection_factory=lambda: self.db), 0)
        self.assertTrue(self.db.closed)
        self.assertEqual(self.db.calls[0][0], load_products.SCHEMA.read_text(encoding="utf-8"))

    def test_driver_is_lazy_and_connect_settings_match_existing_style(self):
        settings = SimpleNamespace(mysql_host="fake-host", mysql_port=3306, mysql_user="fake-user",
            mysql_database="fake-db", mysql_password=SimpleNamespace(get_secret_value=lambda: "fake-secret"))
        driver = SimpleNamespace(connect=lambda **kwargs: kwargs)
        with patch.object(load_products.importlib, "import_module", return_value=driver):
            kwargs = load_products.connect_mysql(settings)
        self.assertEqual(kwargs["charset"], "utf8mb4")
        self.assertFalse(kwargs["autocommit"])
        self.assertEqual(kwargs["password"], "fake-secret")
        with patch.object(load_products.importlib, "import_module", side_effect=ImportError()):
            with self.assertRaisesRegex(RuntimeError, "pymysql"):
                load_products.connect_mysql(settings)

    def test_default_connection_reads_only_process_environment(self):
        driver = SimpleNamespace(connect=lambda **kwargs: kwargs)
        with patch.dict("os.environ", {"MYSQL_HOST": "offline", "MYSQL_PORT": "3307"}, clear=True), \
                patch.object(load_products.importlib, "import_module", return_value=driver):
            kwargs = load_products.connect_mysql()
        self.assertEqual(kwargs["host"], "offline")
        self.assertEqual(kwargs["port"], 3307)


if __name__ == "__main__":
    unittest.main()
