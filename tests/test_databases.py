import importlib.util
import sys

from sqlalchemy import URL

from alchemy_utils import (
    Database,
    DuckDBDatabase,
    PostgreSQLDatabase,
    SQLiteDatabase,
)


def test_database_factory_selects_independent_engine_subclass(engine):
    database = Database(engine)

    expected = {
        "duckdb": DuckDBDatabase,
        "postgresql": PostgreSQLDatabase,
        "sqlite": SQLiteDatabase,
    }[engine.dialect.name]
    assert type(database) is expected
    assert isinstance(database, Database)
    if engine.dialect.name == "duckdb":
        assert not isinstance(database, PostgreSQLDatabase)


def test_database_factory_preserves_existing_engine(engine):
    assert Database(engine).engine is engine


def test_database_accepts_sqlalchemy_url():
    url = URL.create("sqlite+pysqlite", database=":memory:")

    database = Database(url)

    assert isinstance(database, SQLiteDatabase)
    assert database.engine.url.database == ":memory:"


def test_database_accepts_sqlite_memory_shorthand():
    database = Database(":memory:")

    assert isinstance(database, SQLiteDatabase)
    assert database.engine.url.database == ":memory:"


def test_custom_database_subclass_is_not_redispatched():
    class CustomDatabase(Database):
        pass

    database = CustomDatabase("sqlite:///:memory:")

    assert type(database) is CustomDatabase


def test_duckdb_replace_reuses_generated_primary_key_sequence(tmp_path):
    database = Database(f"duckdb:///{tmp_path / 'replace.duckdb'}")
    table = database["people"].create({"id": int, "name": str}, pk="id")

    table.create({"id": int, "name": str}, pk="id", replace=True)
    table.insert({"name": "Ada"})

    assert table.last_pk == 1


def test_duckdb_bulk_insert_temporarily_caches_missing_pandas(tmp_path, monkeypatch):
    database = Database(f"duckdb:///{tmp_path / 'bulk.duckdb'}")
    find_spec_calls = 0

    def missing_pandas(name):
        nonlocal find_spec_calls
        assert name == "pandas"
        find_spec_calls += 1

    monkeypatch.delitem(sys.modules, "pandas", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", missing_pandas)

    with database.bulk_insert_context():
        assert "pandas" in sys.modules
        assert sys.modules["pandas"] is None

    assert "pandas" not in sys.modules
    assert find_spec_calls == 1
