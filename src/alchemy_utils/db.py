from __future__ import annotations

import datetime
import decimal
import itertools
import pathlib
import uuid
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import sqlalchemy as sa
from sqlalchemy.engine import URL, Engine
from sqlalchemy.schema import CreateTable
from typing_extensions import Self


class NoTable(Exception):
    """The requested table does not exist."""


class NotFoundError(Exception):
    """No row matched the supplied primary key."""


class PrimaryKeyRequired(Exception):
    """The operation needs a declared primary key."""


class InvalidColumns(Exception):
    """One or more input columns do not exist on the target table."""


class Column(NamedTuple):
    """Compatibility-shaped description of an introspected column."""

    cid: int
    name: str
    type: str
    notnull: int
    default_value: Any
    is_pk: int


@dataclass(order=True, frozen=True)
class ForeignKey:
    """A reflected single- or multi-column foreign key."""

    table: str
    column: str | None = field(compare=False)
    other_table: str
    other_column: str | None = field(compare=False)
    columns: tuple[str, ...] = ()
    other_columns: tuple[str, ...] = ()
    is_compound: bool = False
    on_delete: str = "NO ACTION"
    on_update: str = "NO ACTION"


class Index(NamedTuple):
    """Compatibility-shaped description of an explicit secondary index."""

    seq: int
    name: str
    unique: int
    origin: str
    partial: int
    columns: list[str]


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
    if types <= {dict, list, tuple}:
        return dict
    if len(types) == 1 and next(iter(types)) in _PYTHON_TYPES:
        return next(iter(types))
    return str


class Database:
    """Small sqlite-utils-shaped wrapper around a SQLAlchemy engine."""

    def __new__(cls, engine_or_url: Engine | URL | str | pathlib.Path) -> Self:
        if cls is Database:
            engine = cls._coerce_engine(engine_or_url)
            # Lazy imports avoid a cycle: dialect subclasses inherit this class.
            from .databases.duckdb import DuckDBDatabase
            from .databases.postgresql import PostgreSQLDatabase
            from .databases.sqlite import SQLiteDatabase

            database_class = {
                "duckdb": DuckDBDatabase,
                "postgresql": PostgreSQLDatabase,
                "sqlite": SQLiteDatabase,
            }.get(engine.dialect.name, Database)
            instance = super().__new__(database_class)
            instance._factory_engine = engine
            return instance
        return super().__new__(cls)

    def __init__(self, engine_or_url: Engine | URL | str | pathlib.Path):
        if hasattr(self, "_factory_engine"):
            self.engine = self._factory_engine
            del self._factory_engine
        else:
            self.engine = self._coerce_engine(engine_or_url)

    @staticmethod
    def _coerce_engine(engine_or_url: Engine | URL | str | pathlib.Path) -> Engine:
        if isinstance(engine_or_url, Engine):
            return engine_or_url
        if isinstance(engine_or_url, URL):
            return sa.create_engine(engine_or_url)
        value = str(engine_or_url)
        if "://" not in value:
            value = f"sqlite+pysqlite:///{value}"
        return sa.create_engine(value)

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

    def create_table(
        self, name: str, columns: Mapping[str, Any], **kwargs: Any
    ) -> Table:
        return self.table(name).create(columns, **kwargs)

    def reflect_table(
        self, table_name: str, *, metadata: sa.MetaData | None = None
    ) -> sa.Table:
        return sa.Table(
            table_name, metadata or sa.MetaData(), autoload_with=self.engine
        )

    def primary_keys(self, table_name: str) -> list[str]:
        return list(
            sa.inspect(self.engine)
            .get_pk_constraint(table_name)
            .get("constrained_columns")
            or []
        )

    def introspect_columns(self, table_name: str) -> list[dict[str, Any]]:
        return sa.inspect(self.engine).get_columns(table_name)

    def introspect_foreign_keys(self, table_name: str) -> list[dict[str, Any]]:
        return sa.inspect(self.engine).get_foreign_keys(table_name)

    def introspect_indexes(self, table_name: str) -> list[dict[str, Any]]:
        return sa.inspect(self.engine).get_indexes(table_name)

    def table_schema(self, table_name: str) -> str:
        table = self.reflect_table(table_name)
        return str(CreateTable(table).compile(self.engine)).strip()

    def view_schema(self, view_name: str) -> str:
        definition = sa.inspect(self.engine).get_view_definition(view_name)
        if definition is None:
            raise NoTable(f"View {view_name} does not exist")
        preparer = self.engine.dialect.identifier_preparer
        return f"CREATE VIEW {preparer.quote(view_name)} AS {definition}".strip()

    def drop_table(self, table_name: str) -> None:
        self.reflect_table(table_name).drop(self.engine)

    def supports_rowid(self) -> bool:
        return False

    def insert_statement(self, table: sa.Table) -> Any:
        raise NotImplementedError(
            f"Inserts are not implemented for {self.engine.dialect.name}"
        )

    @contextmanager
    def bulk_insert_context(self) -> Generator[None, None, None]:
        """Apply dialect-specific setup around a bulk insert."""
        yield

    def insert_ignore_statement(self, table: sa.Table) -> Any:
        del table
        raise NotImplementedError(
            f"Insert ignore is not implemented for {self.engine.dialect.name}"
        )

    def insert_replace_statement(
        self, table: sa.Table, pk_names: list[str], update_names: list[str]
    ) -> Any:
        del table, pk_names, update_names
        raise NotImplementedError(
            f"Insert replace is not implemented for {self.engine.dialect.name}"
        )

    def upsert_statement(
        self, table: sa.Table, pk_names: list[str], update_names: list[str]
    ) -> Any:
        del table, pk_names, update_names
        raise NotImplementedError(
            f"Upsert is not implemented for {self.engine.dialect.name}"
        )

    def add_column(
        self,
        table_name: str,
        column_name: str,
        sql_type: sa.types.TypeEngine[Any],
    ) -> None:
        type_sql = sql_type.compile(self.engine.dialect)
        preparer = self.engine.dialect.identifier_preparer
        sql = f"ALTER TABLE {preparer.quote(table_name)} ADD COLUMN {preparer.quote(column_name)} {type_sql}"
        with self.engine.begin() as connection:
            connection.exec_driver_sql(sql)

    def primary_key_column_options(
        self,
        metadata: sa.MetaData,
        table_name: str,
        column_name: str,
        sql_type: sa.types.TypeEngine[Any],
        pk_count: int,
    ) -> tuple[list[Any], dict[str, Any]]:
        del metadata, table_name, column_name
        return [], {"autoincrement": isinstance(sql_type, sa.Integer) and pk_count == 1}

    def table_names(self) -> list[str]:
        return sa.inspect(self.engine).get_table_names()

    def view_names(self) -> list[str]:
        return sa.inspect(self.engine).get_view_names()

    @property
    def tables(self) -> list[Table]:
        return [self.table(name) for name in self.table_names()]

    @property
    def schema(self) -> str:
        definitions = [self.table_schema(name) for name in self.table_names()]
        definitions.extend(self.view_schema(name) for name in self.view_names())
        return "\n".join(f"{definition};" for definition in definitions)


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
        return self.db.reflect_table(self.name)

    def _primary_keys(self) -> list[str]:
        return self.db.primary_keys(self.name)

    def _effective_pk(self, pk: str | tuple[str, ...] | list[str] | None) -> list[str]:
        value = pk or self._defaults.get("pk") or (self.pks if self.exists() else [])
        return [value] if isinstance(value, str) else list(value or ())

    def _ensure_missing_columns(self, records: Iterable[Mapping[str, Any]]) -> None:
        current = set(self.columns_dict)
        values_by_name: dict[str, list[Any]] = {}
        for record in records:
            for name, value in record.items():
                if name not in current:
                    values_by_name.setdefault(name, []).append(value)
        for name, values in values_by_name.items():
            sql_type = _to_sqlalchemy_type(_suggest_type(values))
            self.db.add_column(self.name, name, sql_type)

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
        del unsupported
        if not columns:
            raise ValueError("Tables must have at least one column")
        if pk is None:
            pk = self._defaults.get("pk")
        if self.exists():
            if replace:
                self.db.drop_table(self.name)
            elif if_not_exists or ignore:
                return self

        pk_names = [pk] if isinstance(pk, str) else list(pk or ())
        not_null_names = set(not_null or ())
        columns = dict(columns)
        if len(pk_names) == 1 and pk_names[0] not in columns:
            columns = {pk_names[0]: int, **columns}

        ordered_names = [name for name in column_order or () if name in columns]
        ordered_names.extend(name for name in columns if name not in ordered_names)

        metadata = sa.MetaData()
        sa_columns = []
        for name in ordered_names:
            is_pk = name in pk_names
            sql_type = _to_sqlalchemy_type(columns[name])
            args: list[Any] = []
            column_kwargs: dict[str, Any] = {
                "primary_key": is_pk and len(pk_names) == 1,
                "nullable": not (is_pk or name in not_null_names),
            }
            if is_pk:
                pk_args, pk_options = self.db.primary_key_column_options(
                    metadata,
                    self.name,
                    name,
                    sql_type,
                    len(pk_names),
                )
                args.extend(pk_args)
                column_kwargs.update(pk_options)
            if defaults and name in defaults:
                default = defaults[name]
                column_kwargs["server_default"] = (
                    default
                    if isinstance(default, sa.sql.ClauseElement)
                    else sa.literal(default)
                )
            sa_columns.append(sa.Column(name, sql_type, *args, **column_kwargs))
        constraints: list[sa.PrimaryKeyConstraint] = []
        if len(pk_names) > 1:
            constraints.append(sa.PrimaryKeyConstraint(*pk_names))
        for foreign_key in foreign_keys or ():
            if len(foreign_key) == 2:
                local_columns, other_table = foreign_key
                other_columns = local_columns
            elif len(foreign_key) == 3:
                local_columns, other_table, other_columns = foreign_key
            else:
                raise ValueError(f"Invalid foreign key: {foreign_key!r}")
            local_names = (
                [local_columns]
                if isinstance(local_columns, str)
                else list(local_columns)
            )
            other_names = (
                [other_columns]
                if isinstance(other_columns, str)
                else list(other_columns)
            )
            if other_table != self.name and other_table not in metadata.tables:
                self.db.reflect_table(other_table, metadata=metadata)
            constraints.append(
                sa.ForeignKeyConstraint(
                    local_names,
                    [f"{other_table}.{name}" for name in other_names],
                )
            )
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
        return self.insert_all([record], pk=pk, **kwargs)

    def insert_all(
        self,
        records: Iterable[Mapping[str, Any]],
        pk: str | tuple[str, ...] | list[str] | None = None,
        *,
        batch_size: int = 100,
        alter: bool = False,
        ignore: bool = False,
        replace: bool = False,
        truncate: bool = False,
        upsert: bool = False,
        stream: bool = False,
        columns: Mapping[str, Any] | None = None,
        **create_kwargs: Any,
    ) -> Self:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        with self.db.bulk_insert_context():
            return self._insert_all(
                records,
                pk,
                batch_size=batch_size,
                alter=alter,
                ignore=ignore,
                replace=replace,
                truncate=truncate,
                upsert=upsert,
                stream=stream,
                columns=columns,
                **create_kwargs,
            )

    def _insert_all(
        self,
        records: Iterable[Mapping[str, Any]],
        pk: str | tuple[str, ...] | list[str] | None,
        *,
        batch_size: int,
        alter: bool,
        ignore: bool,
        replace: bool,
        truncate: bool,
        upsert: bool,
        stream: bool,
        columns: Mapping[str, Any] | None,
        **create_kwargs: Any,
    ) -> Self:
        if ignore and replace:
            raise ValueError("Use either ignore=True or replace=True, not both")
        self.last_pk = None
        self.last_rowid = None
        upsert_pk_names = self._effective_pk(pk) if upsert else []
        if upsert and not upsert_pk_names:
            raise PrimaryKeyRequired("upsert() requires a pk")

        records_iterator = iter(records)
        try:
            first_record = next(records_iterator)
        except StopIteration:
            if truncate and self.exists():
                table = self._sa_table()
                with self.db.engine.begin() as connection:
                    connection.execute(sa.delete(table))
            return self

        try:
            second_record = next(records_iterator)
        except StopIteration:
            single_input = True
            record_source: Iterable[Mapping[str, Any]] = (first_record,)
        else:
            single_input = False
            record_source = itertools.chain(
                (first_record, second_record), records_iterator
            )
        first_record = dict(first_record)
        table_existed = self.exists()

        def record_batches() -> Generator[list[dict[str, Any]], None, None]:
            iterator = iter(record_source)
            while batch := [
                dict(record) for record in itertools.islice(iterator, batch_size)
            ]:
                yield batch

        if alter or (not table_existed and not stream):
            all_records = list(itertools.chain.from_iterable(record_batches()))
            inference_records = all_records

            def batches() -> Generator[list[dict[str, Any]], None, None]:
                iterator = iter(all_records)
                while batch := list(itertools.islice(iterator, batch_size)):
                    yield batch

            batch_iterator: Iterable[list[dict[str, Any]]] = batches()
        else:
            remaining_batches = record_batches()
            first_batch = next(remaining_batches)
            inference_records = first_batch
            batch_iterator = itertools.chain((first_batch,), remaining_batches)

        if not table_existed:
            names = list(
                dict.fromkeys(itertools.chain.from_iterable(inference_records))
            )
            inferred = {
                name: _suggest_type(record.get(name) for record in inference_records)
                for name in names
            }
            inferred.update(columns or {})
            self.create(inferred, pk=pk, **create_kwargs)
        elif alter:
            self._ensure_missing_columns(inference_records)

        table = self._sa_table()
        table_names = [column.name for column in table.columns]
        pk_names = upsert_pk_names if upsert else self.pks
        if ignore:
            statement = self.db.insert_ignore_statement(table)
        elif replace:
            if not pk_names:
                raise PrimaryKeyRequired("replace=True requires a primary key")
            update_names = [name for name in table_names if name not in pk_names]
            statement = self.db.insert_replace_statement(table, pk_names, update_names)
        else:
            statement = self.db.insert_statement(table)
        returned_pk: tuple[Any, ...] | None = None
        result: Any | None = None
        single_generated_pk = (
            single_input
            and bool(pk_names)
            and not all(name in first_record for name in pk_names)
            and not ignore
        )

        with self.db.engine.begin() as connection:
            if truncate:
                connection.execute(sa.delete(table))
            for records_batch in batch_iterator:
                input_names = set().union(*(record.keys() for record in records_batch))
                unknown_names = sorted(input_names.difference(table_names))
                if unknown_names:
                    raise InvalidColumns(
                        f"Invalid column{'s' if len(unknown_names) != 1 else ''} "
                        f"{unknown_names} for table {self.name}"
                    )
                normalized = [
                    {
                        name: record.get(name)
                        for name in table_names
                        if name in record or name in input_names
                    }
                    for record in records_batch
                ]
                # Avoid supplying omitted generated primary keys as explicit NULLs.
                for record, normalized_record in zip(records_batch, normalized):
                    for name in pk_names:
                        if name not in record:
                            normalized_record.pop(name, None)

                if upsert:
                    for record in records_batch:
                        if any(record.get(name) is None for name in pk_names):
                            raise PrimaryKeyRequired(
                                "upsert() requires values for every pk column"
                            )
                    for record in normalized:
                        update_names = [name for name in record if name not in pk_names]
                        upsert_statement = self.db.upsert_statement(
                            table, pk_names, update_names
                        )
                        connection.execute(upsert_statement, record)
                elif single_generated_pk:
                    result = connection.execute(
                        statement.returning(*(table.c[name] for name in pk_names)),
                        normalized[0],
                    )
                    returned_pk = tuple(result.one())
                else:
                    result = connection.execute(statement, normalized)

        if not upsert and single_input:
            original = first_record
            if pk_names and all(name in original for name in pk_names):
                values = tuple(original[name] for name in pk_names)
                self.last_pk = values[0] if len(values) == 1 else values
            elif returned_pk is not None:
                values = returned_pk
                self.last_pk = values[0] if len(values) == 1 else values
            elif result is not None and result.inserted_primary_key:
                values = tuple(result.inserted_primary_key)
                self.last_pk = values[0] if len(values) == 1 else values

        if upsert and single_input:
            values = tuple(first_record[name] for name in pk_names)
            self.last_pk = values[0] if len(values) == 1 else values
        return self

    def upsert(
        self,
        record: Mapping[str, Any],
        pk: str | tuple[str, ...] | list[str] | None = None,
        **kwargs: Any,
    ) -> Self:
        effective_pk = (
            pk or self._defaults.get("pk") or (self.pks if self.exists() else [])
        )
        if not effective_pk:
            raise PrimaryKeyRequired("upsert() requires a pk")
        return self.upsert_all([record], pk=pk, **kwargs)

    def upsert_all(
        self,
        records: Iterable[Mapping[str, Any]],
        pk: str | tuple[str, ...] | list[str] | None = None,
        **kwargs: Any,
    ) -> Self:
        return self.insert_all(records, pk=pk, upsert=True, **kwargs)

    def update(
        self,
        pk_values: Any,
        updates: Mapping[str, Any] | None = None,
        *,
        alter: bool = False,
        conversions: Mapping[str, Any] | None = None,
    ) -> Self:
        if conversions:
            raise NotImplementedError("SQL conversions are not portable")
        self.get(pk_values)
        updates = dict(updates or {})
        if not updates:
            return self
        if alter:
            self._ensure_missing_columns([updates])
        table = self._sa_table()
        values = (
            list(pk_values) if isinstance(pk_values, (list, tuple)) else [pk_values]
        )
        criteria = sa.and_(
            *(table.c[name] == value for name, value in zip(self.pks, values))
        )
        with self.db.engine.begin() as connection:
            connection.execute(sa.update(table).where(criteria).values(updates))
        self.last_pk = values[0] if len(values) == 1 else tuple(values)
        return self

    @property
    def count(self) -> int:
        table = self._sa_table()
        with self.db.engine.connect() as connection:
            return int(connection.scalar(sa.select(sa.func.count()).select_from(table)))

    @property
    def rows(self) -> Generator[dict[str, Any]]:
        table = self._sa_table()
        with self.db.engine.connect() as connection:
            for row in connection.execute(sa.select(table)):
                yield dict(row._mapping)

    def get(self, pk_values: Any) -> dict[str, Any]:
        table = self._sa_table()
        values = (
            list(pk_values) if isinstance(pk_values, (list, tuple)) else [pk_values]
        )
        pks = self.pks
        if not pks or len(pks) != len(values):
            raise NotFoundError
        criteria = sa.and_(
            *(table.c[name] == value for name, value in zip(pks, values))
        )
        with self.db.engine.connect() as connection:
            row = (
                connection.execute(sa.select(table).where(criteria)).mappings().first()
            )
        if row is None:
            raise NotFoundError
        self.last_pk = values[0] if len(values) == 1 else tuple(values)
        return dict(row)

    @property
    def columns(self) -> list[Column]:
        if not self.exists():
            return []
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
            for index, column in enumerate(self.db.introspect_columns(self.name))
        ]

    @property
    def columns_dict(self) -> dict[str, type[Any]]:
        if not self.exists():
            return {}
        result = {}
        for column in self.db.introspect_columns(self.name):
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
    def use_rowid(self) -> bool:
        return not self.pks and self.db.supports_rowid()

    @property
    def default_values(self) -> dict[str, Any]:
        return {
            column.name: column.default_value
            for column in self.columns
            if column.default_value is not None
        }

    @property
    def foreign_keys(self) -> list[ForeignKey]:
        if not self.exists():
            return []
        result = []
        for reflected in self.db.introspect_foreign_keys(self.name):
            columns = tuple(reflected.get("constrained_columns") or ())
            other_columns = tuple(reflected.get("referred_columns") or ())
            compound = len(columns) > 1
            options = reflected.get("options") or {}
            result.append(
                ForeignKey(
                    table=self.name,
                    column=None if compound else columns[0],
                    other_table=reflected["referred_table"],
                    other_column=None if compound else other_columns[0],
                    columns=columns,
                    other_columns=other_columns,
                    is_compound=compound,
                    on_delete=options.get("ondelete", "NO ACTION"),
                    on_update=options.get("onupdate", "NO ACTION"),
                )
            )
        return result

    @property
    def indexes(self) -> list[Index]:
        if not self.exists():
            return []
        return [
            Index(
                seq=sequence,
                name=reflected["name"],
                unique=int(reflected.get("unique", False)),
                origin="c",
                partial=int(
                    any(
                        (reflected.get("dialect_options") or {}).get(option)
                        is not None
                        for option in ("sqlite_where", "postgresql_where")
                    )
                ),
                columns=list(reflected.get("column_names") or ()),
            )
            for sequence, reflected in enumerate(self.db.introspect_indexes(self.name))
        ]

    @property
    def schema(self) -> str:
        return self.db.table_schema(self.name)
