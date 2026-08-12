from sqlalchemy_utils import (
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
