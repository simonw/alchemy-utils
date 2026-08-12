# Research spike: sqlite-utils API on SQLAlchemy

## Conclusion

Building a useful multi-engine library with the core sqlite-utils API is
feasible. SQLAlchemy Core provides enough common ground for table creation,
ordinary inserts and updates, transaction boundaries, identifier quoting, and
most reflection. A thin per-engine adapter is still required; upsert is not a
generic SQLAlchemy operation, and DuckDB needs several concrete workarounds.

The prototype proves the requested surface on SQLite, PostgreSQL, and DuckDB:

- `create`, `insert`, `insert_all`, `upsert`, `upsert_all`, and `update`;
- automatic schema inference and generated integer IDs;
- single and compound primary/foreign keys;
- table, column, PK, FK, explicit-index, default, and schema introspection;
- single-record `last_pk`, mapping/JSON round trips, conflict options,
  identifier quoting, ordered duplicate upserts, and atomic bulk writes.

The installed `alchemy-utils` command exercises that same portable
surface against filenames or SQLAlchemy URLs. It covers create, JSON/JSONL/CSV/
TSV insert and upsert, primary-key update, row reads, and normalized table,
column, foreign-key, index, and schema introspection. Mutations retain the
silent-success convention of the sqlite-utils CLI.

The architecture should not attempt to make SQLAlchemy itself disappear. The
portable contract needs to be explicitly defined where sqlite-utils currently
exposes SQLite-specific behavior.

## Reference work

The API contract was derived from the local `~/dev/sqlite-utils` checkout,
version 4.1.1 at commit `43d5d33` (with unrelated local working-tree changes).
The most relevant implementation is in `sqlite_utils/db.py`; the highest-value
reference tests are `tests/test_create.py`, `tests/test_upsert.py`,
`tests/test_update.py`, `tests/test_introspect.py`, `tests/test_foreign_keys.py`,
and `tests/test_list_mode.py`.

The disposable PostgreSQL harness follows the pattern in
`~/dev/django-sql-dashboard/pytest_plugins/pytest_use_postgresql.py`: one
session-scoped `testing.postgresql` server, with isolated databases managed by
pytest. This spike improves isolation further by creating a fresh named
database for each PostgreSQL test.

## What is genuinely portable

SQLAlchemy Core handles these well:

- quoting arbitrary table and column names;
- Python/SQLAlchemy type declarations;
- single and compound constraints;
- reflected columns, nullability, primary keys, and foreign keys on SQLite and
  PostgreSQL;
- ordinary `INSERT`, `UPDATE`, `SELECT`, and transaction contexts;
- server-side `RETURNING` for generated keys on the tested engines.

The shared implementation can therefore own record normalization, schema
inference, chaining and `last_pk` behavior, PK validation, and the public API.

## Where adapters are required

### Upsert and conflict handling

SQLAlchemy's generic `insert()` has no `on_conflict_do_update()` API. SQLite
and PostgreSQL each expose dialect-specific insert objects. DuckDB accepts
PostgreSQL-style `ON CONFLICT`, so its adapter uses that statement shape too.

Bulk upserts must preserve input order. A single multi-values statement with
duplicate keys diverges by engine: PostgreSQL rejects it, while DuckDB can keep
the first value. This prototype executes ordered upserts inside one transaction,
which gives consistent last-write-wins behavior.

It also builds an update set for each input shape. This preserves fields omitted
from a single partial upsert. Like current sqlite-utils batching, heterogeneous
records share a union of columns and missing members become `NULL`; that subtle
compatibility behavior is tested and should be reconsidered before declaring a
new long-term contract.

### DuckDB generated primary keys

duckdb-engine inherits PostgreSQL DDL behavior and renders a normal integer
primary key as `SERIAL`; DuckDB rejects that type. `IDENTITY` plus a primary-key
constraint is also unsupported in the tested DuckDB release.

The working solution is a DuckDB `Sequence` used as the server default. The
DuckDB subclass owns sequence creation and cleans it up when `replace=True`
drops a table. Inserts use `RETURNING` so `last_pk` remains portable.

### DuckDB reflection

With duckdb-engine 0.17.0:

- column and foreign-key reflection generally works;
- primary-key reflection returns no constrained columns;
- index reflection returns an empty list and emits a warning;
- native JSON reflects as `VARCHAR`, preventing SQLAlchemy's JSON decoder;
- update row counts are not reliable.

The DuckDB subclass uses `information_schema` and DuckDB's catalog functions:

- ordered keys from `table_constraints` and `key_column_usage`;
- native types from `duckdb_columns()`;
- explicit indexes from `duckdb_indexes()`;
- native table DDL from `duckdb_tables()`.

`update()` validates existence with `get()` before issuing the update, so it
does not depend on DuckDB row counts.

## Portable introspection contract

The spike uses these semantics:

- `pks` contains declared key columns in constraint order;
- `columns` preserves physical declaration order and marks PK ordinal position;
- `columns_dict` exposes Python types rather than trying to normalize every
  engine's SQL type spelling;
- one compound FK is one `ForeignKey` object with tuple fields;
- `indexes` means explicit secondary indexes, not engine-created PK/unique
  indexes;
- `schema` is useful DDL, not byte-for-byte original source;
- `default_values` contains reflected server-default expressions and is
  therefore engine-shaped;
- `use_rowid` is true only for a keyless SQLite table, but `pks` does not invent
  `rowid` on any engine.

This is narrower than sqlite-utils' SQLite-specific introspection, but it is a
contract that all three engines can honestly implement.

## Recommended production plan

A production v0.1 should keep the current layout:

```text
db.py                         shared Database factory, Table API, value logic
databases/sqlite.py           SQLite statements and optional exact metadata
databases/postgresql.py       PostgreSQL statements and catalog refinements
databases/duckdb.py           DuckDB statements, sequences, catalog fallbacks
```

Recommended next work, in order:

1. Define and publish the precise compatibility contract, especially rowid,
   defaults, inferred types, heterogeneous bulk rows, and exception types.
2. Add streaming chunks while preserving per-call transaction semantics and
   deterministic duplicate-upsert ordering.
3. Centralize value adaptation for mixed types, dates, decimals, UUIDs,
   memoryviews, and JSON so inferred schemas behave identically on PostgreSQL.
4. Add a reusable transaction API, nested savepoints, and failure tests around
   `truncate` plus `alter`.
5. Complete create options: FK actions, unique/check constraints, and named
   schemas. Decide whether transform/conversion features belong in the portable
   core or in adapters.
6. Run CI across supported Python versions, PostgreSQL versions, SQLite, and a
   pinned DuckDB/duckdb-engine pair.
7. Add compatibility tests adapted directly from sqlite-utils rather than
   expanding behavior only from new examples.

Roughly, an experienced engineer could harden this demonstrated core into a
documented v0.1 in about three to five weeks. That includes streaming,
transactions, value coercion, a broader compatibility suite, CI, and release
work. Pursuing broad sqlite-utils parity—including transforms, hash IDs,
extracts, conversions, FTS, trigger/check details, and SQLite CLI behavior—is a
larger multi-month effort and should be scoped separately.

## Dependency and release notes

The spike deliberately uses the distribution name `alchemy-utils` and
module `alchemy_utils`. The initially tempting name
`sqlalchemy-utils` is already occupied by a different established project.

SQLAlchemy is the only required database package. PostgreSQL and DuckDB drivers
are optional extras. The DuckDB pair should stay tightly pinned and tested
together because duckdb-engine relies on PostgreSQL dialect internals and its
reflection support is incomplete.

Useful primary documentation:

- [SQLAlchemy reflection](https://docs.sqlalchemy.org/en/20/core/reflection.html)
- [SQLAlchemy PostgreSQL upsert](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)
- [SQLAlchemy SQLite upsert](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#insert-on-conflict-upsert)
- [DuckDB INSERT / ON CONFLICT](https://duckdb.org/docs/current/sql/statements/insert)
- [DuckDB metadata functions](https://duckdb.org/docs/current/sql/meta/duckdb_table_functions)
- [duckdb-engine caveats](https://github.com/Mause/duckdb_engine#things-to-keep-in-mind)
- [testing.postgresql](https://pypi.org/project/testing.postgresql/)
