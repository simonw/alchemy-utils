# sqlite-utils-sqlalchemy

An executable research spike for a subset of the
[`sqlite-utils`](https://sqlite-utils.datasette.io/) Python API backed by
SQLAlchemy Core.

It demonstrates the same style of table-first API across SQLite, PostgreSQL,
and DuckDB:

```python
from sqlite_utils_sqlalchemy import Database

db = Database("sqlite:///:memory:")

people = db["people"].insert(
    {"id": 1, "name": "Ada", "profile": {"language": "Python"}},
    pk="id",
)
people.upsert({"id": 1, "name": "Ada Lovelace"})
people.insert_all(
    [
        {"id": 2, "name": "Grace"},
        {"id": 3, "name": "Katherine"},
    ]
)
people.update(2, {"name": "Grace Hopper"})

assert people.pks == ["id"]
assert people.columns_dict["profile"] is dict
assert people.get(1)["name"] == "Ada Lovelace"
```

Swap only the URL to use another engine:

```python
postgres = Database("postgresql+psycopg://user:password@localhost/app")
duckdb = Database("duckdb:///analytics.duckdb")
```

This is a spike, not a published compatibility promise. See
[RESEARCH.md](RESEARCH.md) for the conclusion, design trade-offs, and a rough
production estimate.

## Implemented API

`Database` supports:

- construction from a SQLAlchemy `Engine`, `URL`, URL string, or SQLite path;
- `db[name]`, `db.table()`, and `db.create_table()`;
- `table_names()`, `view_names()`, `tables`, and normalized `schema`;
- context-manager cleanup and `close()`.

`Table` supports:

- `create()` with Python or SQLAlchemy types, single/compound primary keys,
  partial column ordering, `NOT NULL`, server defaults, single/compound foreign
  keys, and existing-table options;
- `insert()`, `insert_all()`, `upsert()`, `upsert_all()`, and `update()`;
- generated integer primary keys on all three engines;
- `alter=True` for new nullable columns, plus insert `ignore=True` and
  `replace=True` conflict modes;
- `exists()`, `count`, `rows`, and `get()`;
- `columns`, `columns_dict`, `pks`, `foreign_keys`, `indexes`, `schema`,
  `default_values`, and `use_rowid`.

All mutation methods return the same `Table` object for chaining. A one-record
write sets `last_pk`; bulk writes leave it as `None`.

## Engine-specific architecture

Calling `Database(...)` selects an independent engine implementation:

```text
Database factory
├── SQLiteDatabase       SQLite ON CONFLICT and rowid capability
├── PostgreSQLDatabase   PostgreSQL ON CONFLICT
└── DuckDBDatabase       DuckDB ON CONFLICT, sequences, catalog fallbacks
         │
         └── PK / JSON / index / DDL reflection repairs

Table                     shared API and orchestration only
```

The shared `Table` class does not inspect dialect names or build dialect SQL.
It delegates conflict statements, generated-key DDL, table lifecycle, and
reflection to its `Database` instance.

## Install

The base package needs SQLAlchemy and works with Python 3.10 or later. Engine
drivers are extras:

```bash
pip install 'sqlite-utils-sqlalchemy[postgresql]'
pip install 'sqlite-utils-sqlalchemy[duckdb]'
```

For this checkout, `uv sync` installs the development group, including both
drivers and the test tools.

## Test

```bash
uv sync
uv run ruff check src tests
uv run pytest
```

PostgreSQL tests use `testing.postgresql` to start one disposable server and a
unique database per test. They never use an existing application database. The
fixture finds `postgres` and `initdb` from environment variables, `PATH`, or
`pg_config`; if it cannot find them, PostgreSQL cases are skipped.

Homebrew example:

```bash
PG_BIN="$(brew --prefix postgresql@18)/bin"
POSTGRESQL_PATH="$PG_BIN/postgres" \
INITDB_PATH="$PG_BIN/initdb" \
uv run pytest
```

The current local run covers 91 cases. It was exercised with Python 3.14.3,
SQLAlchemy 2.0.52, SQLite 3.50.4, PostgreSQL 18.3, DuckDB 1.5.5,
duckdb-engine 0.17.0, and psycopg 3.3.4.

## Deliberate spike limitations

- Bulk inputs are materialized in memory; `batch_size` is accepted but not yet
  used for streaming chunks.
- Bulk input supports mappings, not sqlite-utils' header-plus-sequence mode.
- `alter=True` only adds nullable columns. Full transforms are out of scope.
- Exact SQLite DDL text, implicit indexes, triggers, checks, FTS, and STRICT
  metadata are not portable and are not emulated.
- `hash_id`, extracts, conversions, `analyze`, and schema transforms are not
  implemented.
- Reflected type names and raw server-default SQL vary by engine. The stable
  portable fields are names, Python types, nullability, key ordering, foreign
  key shape, and explicit indexes.
- A hidden `rowid` is never synthesized as a primary key. `use_rowid` reports
  the SQLite capability, while `pks` returns only declared keys on every engine.
- DuckDB expression-index parsing is intentionally best-effort; ordinary
  column indexes are covered.
