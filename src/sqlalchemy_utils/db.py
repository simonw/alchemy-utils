from __future__ import annotations

import datetime
import decimal
import pathlib
import uuid
from collections.abc import Generator, Iterable, Mapping
from typing import Any, NamedTuple, Self

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable


class NoTable(Exception):
    """The requested table does not exist."""


class NotFoundError(Exception):
    """No row matched the supplied primary key."""


class PrimaryKeyRequired(Exception):
    """The operation needs a declared primary key."""


class Column(NamedTuple):
    """Compatibility-shaped description of an introspected column."""

    cid: int
    name: str
    type: str
    notnull: int
    default_value: Any
    is_pk: int


_PYTHON_TYPES: dict[Any, type[sa.types.TypeEngine[Any]]] = {
    bool: sa.Boolean,
    bytes: sa.LargeBinary,
    datetime.date: sa.Date,
    datetime.datetime: sa.DateTime,
    datetime.time: sa.Time,
    decimal.Decimal: sa.Numeric,
    dict: sa.JSON,
    float: sa.Float,
    int: sa.Integer,
    list: sa.JSON,
    str: sa.Text,
    tuple: sa.JSON,
    uuid.UUID: sa.Uuid,
    type(None): sa.Text,
}

_STRING_TYPES: dict[str, type[sa.types.TypeEngine[Any]]] = {
    "BLOB": sa.LargeBinary,
    "BOOLEAN": sa.Boolean,
    "DATE": sa.Date,
    "DATETIME": sa.DateTime,
    "FLOAT": sa.Float,
    "INTEGER": sa.Integer,
    "JSON": sa.JSON,
    "NUMERIC": sa.Numeric,
    "REAL": sa.Float,
    "TEXT": sa.Text,
    "TIME": sa.Time,
}


def _to_sqlalchemy_type(value: Any) -> sa.types.TypeEngine[Any]:
    if isinstance(value, sa.types.TypeEngine):
        return value
    if isinstance(value, type) and issubclass(value, sa.types.TypeEngine):
        return value()
    if value in _PYTHON_TYPES:
        return _PYTHON_TYPES[value]()
    if isinstance(value, str) and value.upper() in _STRING_TYPES:
        return _STRING_TYPES[value.upper()]()
    raise TypeError(f"Unsupported column type: {value!r}")


def _suggest_type(values: Iterable[Any]) -> type[Any]:
    non_null = [value for value in values if value is not None]
    if not non_null:
        return str
    types = {type(value) for value in non_null}
    if types <= {bool}:
        return bool
    if types <= {bool, int}:
        return int
    if types <= {bool, int, float}:
        return float
    if len(types) == 1 and next(iter(types)) in _PYTHON_TYPES:
        return next(iter(types))
    return str


class Database:
    """Small sqlite-utils-shaped wrapper around a SQLAlchemy engine."""

    def __init__(self, engine_or_url: Engine | str | pathlib.Path):
        if isinstance(engine_or_url, Engine):
            self.engine = engine_or_url
        else:
            value = str(engine_or_url)
            if "://" not in value:
                value = f"sqlite+pysqlite:///{value}"
            self.engine = sa.create_engine(value)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.engine.dispose()

    def __getitem__(self, table_name: str) -> Table:
        return self.table(table_name)

    def __repr__(self) -> str:
        return f"<Database {self.engine.url}>"

    def table(self, table_name: str, **kwargs: Any) -> Table:
        return Table(self, table_name, **kwargs)

    def table_names(self) -> list[str]:
        return sa.inspect(self.engine).get_table_names()

    def view_names(self) -> list[str]:
        return sa.inspect(self.engine).get_view_names()

    @property
    def tables(self) -> list[Table]:
        return [self.table(name) for name in self.table_names()]


class Table:
    last_pk: Any | None = None
    last_rowid: int | None = None

    def __init__(
        self,
        db: Database,
        name: str,
        pk: str | tuple[str, ...] | list[str] | None = None,
        **defaults: Any,
    ):
        self.db = db
        self.name = name
        self._defaults = {"pk": pk, **defaults}
        self.last_pk = None
        self.last_rowid = None

    def __repr__(self) -> str:
        suffix = (
            f" ({', '.join(column.name for column in self.columns)})"
            if self.exists()
            else " (does not exist yet)"
        )
        return f"<Table {self.name}{suffix}>"

    def exists(self) -> bool:
        return sa.inspect(self.db.engine).has_table(self.name)

    def _sa_table(self) -> sa.Table:
        if not self.exists():
            raise NoTable(f"Table {self.name} does not exist")
        table = sa.Table(self.name, sa.MetaData(), autoload_with=self.db.engine)
        if self.db.engine.dialect.name == "duckdb":
            # duckdb-engine 0.17 reflects DuckDB's native JSON as VARCHAR,
            # which skips SQLAlchemy's JSON result decoder. Patch that known
            # reflection gap using DuckDB's catalog table.
            with self.db.engine.connect() as connection:
                native_types = {
                    row.column_name: row.data_type
                    for row in connection.execute(
                        sa.text(
                            """
                            select column_name, data_type
                            from duckdb_columns()
                            where schema_name = :schema and table_name = :table_name
                            """
                        ),
                        {
                            "schema": sa.inspect(self.db.engine).default_schema_name
                            or "main",
                            "table_name": self.name,
                        },
                    )
                }
            for column in table.columns:
                if native_types.get(column.name) == "JSON":
                    column.type = sa.JSON()
        return table

    def _primary_keys(self) -> list[str]:
        inspector = sa.inspect(self.db.engine)
        reflected = list(
            inspector.get_pk_constraint(self.name).get("constrained_columns") or []
        )
        if reflected or self.db.engine.dialect.name != "duckdb":
            return reflected

        # duckdb-engine 0.17 does not reflect indexes or primary keys yet.
        # DuckDB's standard information_schema views preserve compound-key order.
        query = sa.text(
            """
            select kcu.column_name
            from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu
              on tc.constraint_catalog = kcu.constraint_catalog
             and tc.constraint_schema = kcu.constraint_schema
             and tc.constraint_name = kcu.constraint_name
            where tc.table_schema = :schema
              and tc.table_name = :table_name
              and tc.constraint_type = 'PRIMARY KEY'
            order by kcu.ordinal_position
            """
        )
        with self.db.engine.connect() as connection:
            return list(
                connection.scalars(
                    query,
                    {
                        "schema": inspector.default_schema_name or "main",
                        "table_name": self.name,
                    },
                )
            )

    def create(
        self,
        columns: Mapping[str, Any],
        pk: str | tuple[str, ...] | list[str] | None = None,
        foreign_keys: Iterable[Any] | None = None,
        column_order: list[str] | None = None,
        not_null: Iterable[str] | None = None,
        defaults: Mapping[str, Any] | None = None,
        *,
        if_not_exists: bool = False,
        replace: bool = False,
        ignore: bool = False,
        **unsupported: Any,
    ) -> Self:
        del foreign_keys, defaults, unsupported
        if pk is None:
            pk = self._defaults.get("pk")
        if self.exists():
            if replace:
                self._sa_table().drop(self.db.engine)
            elif if_not_exists or ignore:
                return self

        pk_names = [pk] if isinstance(pk, str) else list(pk or ())
        not_null_names = set(not_null or ())
        ordered_names = list(column_order or ())
        ordered_names.extend(name for name in columns if name not in ordered_names)

        metadata = sa.MetaData()
        sa_columns = []
        for name in ordered_names:
            is_pk = name in pk_names
            sa_columns.append(
                sa.Column(
                    name,
                    _to_sqlalchemy_type(columns[name]),
                    primary_key=is_pk and len(pk_names) == 1,
                    nullable=False if is_pk or name in not_null_names else True,
                    # Avoid duckdb-engine rendering integer PKs as SERIAL.
                    autoincrement=False if is_pk else "auto",
                )
            )
        constraints: list[sa.PrimaryKeyConstraint] = []
        if len(pk_names) > 1:
            constraints.append(sa.PrimaryKeyConstraint(*pk_names))
        table = sa.Table(self.name, metadata, *sa_columns, *constraints)
        table.create(self.db.engine, checkfirst=if_not_exists or ignore)
        self._defaults["pk"] = pk
        return self

    def insert(
        self,
        record: Mapping[str, Any],
        pk: str | tuple[str, ...] | list[str] | None = None,
        **kwargs: Any,
    ) -> Self:
        if not self.exists():
            self.create(
                {name: _suggest_type([value]) for name, value in record.items()},
                pk=pk,
                **kwargs,
            )
        table = self._sa_table()
        with self.db.engine.begin() as connection:
            result = connection.execute(sa.insert(table).values(dict(record)))
        pks = self.pks
        if pks and all(name in record for name in pks):
            values = tuple(record[name] for name in pks)
            self.last_pk = values[0] if len(values) == 1 else values
        elif result.inserted_primary_key:
            values = tuple(result.inserted_primary_key)
            self.last_pk = values[0] if len(values) == 1 else values
        return self

    def upsert(
        self,
        record: Mapping[str, Any],
        pk: str | tuple[str, ...] | list[str] | None = None,
        **kwargs: Any,
    ) -> Self:
        del kwargs
        effective_pk = pk or self._defaults.get("pk") or (self.pks if self.exists() else [])
        if not effective_pk:
            raise PrimaryKeyRequired("upsert() requires a pk")
        raise NotImplementedError

    @property
    def count(self) -> int:
        table = self._sa_table()
        with self.db.engine.connect() as connection:
            return int(connection.scalar(sa.select(sa.func.count()).select_from(table)))

    @property
    def rows(self) -> Generator[dict[str, Any], None, None]:
        table = self._sa_table()
        with self.db.engine.connect() as connection:
            for row in connection.execute(sa.select(table)):
                yield dict(row._mapping)

    def get(self, pk_values: Any) -> dict[str, Any]:
        table = self._sa_table()
        values = list(pk_values) if isinstance(pk_values, (list, tuple)) else [pk_values]
        pks = self.pks
        if not pks or len(pks) != len(values):
            raise NotFoundError
        criteria = sa.and_(*(table.c[name] == value for name, value in zip(pks, values)))
        with self.db.engine.connect() as connection:
            row = connection.execute(sa.select(table).where(criteria)).mappings().first()
        if row is None:
            raise NotFoundError
        self.last_pk = values[0] if len(values) == 1 else tuple(values)
        return dict(row)

    @property
    def columns(self) -> list[Column]:
        if not self.exists():
            return []
        inspector = sa.inspect(self.db.engine)
        pk_names = self._primary_keys()
        pk_positions = {name: index + 1 for index, name in enumerate(pk_names)}
        return [
            Column(
                cid=index,
                name=column["name"],
                type=str(column["type"]),
                notnull=int(not column.get("nullable", True)),
                default_value=column.get("default"),
                is_pk=pk_positions.get(column["name"], 0),
            )
            for index, column in enumerate(inspector.get_columns(self.name))
        ]

    @property
    def columns_dict(self) -> dict[str, type[Any]]:
        if not self.exists():
            return {}
        result = {}
        for column in sa.inspect(self.db.engine).get_columns(self.name):
            try:
                python_type = column["type"].python_type
            except NotImplementedError:
                python_type = object
            result[column["name"]] = python_type
        return result

    @property
    def pks(self) -> list[str]:
        if not self.exists():
            return []
        return self._primary_keys()

    @property
    def schema(self) -> str:
        table = self._sa_table()
        return str(CreateTable(table).compile(self.db.engine)).strip()
