from __future__ import annotations

import base64
import binascii
import csv as csv_stdlib
import datetime
import decimal
import functools
import json
import pathlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from typing import Any, TextIO, TypeVar, cast

import click
import sqlalchemy as sa

from . import (
    Database,
    InvalidColumns,
    NoTable,
    NotFoundError,
    PrimaryKeyRequired,
)

VALID_COLUMN_TYPES = (
    "BLOB",
    "BOOLEAN",
    "DATE",
    "DATETIME",
    "FLOAT",
    "INTEGER",
    "JSON",
    "NUMERIC",
    "REAL",
    "TEXT",
    "TIME",
)

F = TypeVar("F", bound=Callable[..., Any])


def _database_target(value: str) -> str | pathlib.Path:
    if "://" in value:
        return value
    return pathlib.Path(value).expanduser()


@contextmanager
def _database(value: str):
    try:
        database = Database(_database_target(value))
    except ModuleNotFoundError as ex:
        dialect = value.split(":", 1)[0].split("+", 1)[0]
        extra = {"duckdb": "duckdb", "postgresql": "postgresql"}.get(dialect)
        if extra:
            raise click.ClickException(
                f"The {dialect} driver is not installed; install "
                f"sqlite-utils-sqlalchemy[{extra}]"
            ) from ex
        raise click.ClickException(f"Database driver is not installed: {ex.name}") from ex
    try:
        yield database
    finally:
        database.close()


def _translate_errors(function: F) -> F:
    @functools.wraps(function)
    def inner(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except click.ClickException:
            raise
        except (InvalidColumns, NoTable, NotFoundError, PrimaryKeyRequired) as ex:
            message = str(ex) or ex.__class__.__name__
            raise click.ClickException(message) from ex
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as ex:
            raise click.ClickException(str(ex)) from ex
        except sa.exc.SQLAlchemyError as ex:
            message = str(getattr(ex, "orig", ex)).replace("\n", " ")
            raise click.ClickException(message) from ex

    return cast(F, inner)


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "$base64": True,
            "encoded": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "_asdict"):
        return value._asdict()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any, *, compact: bool = False) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":") if compact else None,
        indent=None if compact else 2,
    )


def _echo_json(value: Any, *, nl: bool = False) -> None:
    if nl:
        for item in value:
            click.echo(_json_dumps(item, compact=True))
    else:
        click.echo(_json_dumps(value))


def _parse_json_or_string(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _parse_defaults(values: Sequence[tuple[str, str]]) -> dict[str, Any]:
    return {name: _parse_json_or_string(value) for name, value in values}


def _parse_column_pairs(columns: Sequence[str]) -> dict[str, str]:
    if len(columns) % 2:
        raise click.ClickException(
            "columns must be an even number of 'name' 'type' pairs"
        )
    parsed = {}
    for name, type_name in zip(columns[::2], columns[1::2]):
        normalized = type_name.upper()
        if normalized not in VALID_COLUMN_TYPES:
            allowed = ", ".join(VALID_COLUMN_TYPES)
            raise click.ClickException(f"column types must be one of: {allowed}")
        parsed[name] = normalized
    return parsed


def _selected_input_format(
    filename: str, *, nl: bool, csv: bool, tsv: bool
) -> str:
    selected = [name for name, enabled in (("nl", nl), ("csv", csv), ("tsv", tsv)) if enabled]
    if len(selected) > 1:
        raise click.ClickException("Use only one of --nl, --csv, or --tsv")
    if selected:
        return selected[0]
    suffix = pathlib.Path(filename).suffix.lower() if filename != "-" else ""
    return {
        ".csv": "csv",
        ".jsonl": "nl",
        ".ndjson": "nl",
        ".tsv": "tsv",
    }.get(suffix, "json")


def _verify_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise click.ClickException("Each input record must be a JSON object")
    return {name: _decode_json_value(item) for name, item in value.items()}


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if value.get("$base64") is True and "encoded" in value:
            encoded = value["encoded"]
            if not isinstance(encoded, str):
                raise click.ClickException("Base64 encoded values must be strings")
            try:
                return base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as ex:
                raise click.ClickException("Invalid base64 encoded value") from ex
        return {name: _decode_json_value(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_decode_json_value(item) for item in value]
    return value


def _read_records(
    filename: str, *, nl: bool, csv: bool, tsv: bool
) -> tuple[list[dict[str, Any]], bool]:
    input_format = _selected_input_format(filename, nl=nl, csv=csv, tsv=tsv)
    with click.open_file(filename, mode="r", encoding="utf-8-sig") as file:
        stream = cast(TextIO, file)
        if input_format == "json":
            value = json.load(stream)
            if isinstance(value, Mapping):
                return [_verify_record(value)], True
            if not isinstance(value, list):
                raise click.ClickException(
                    "JSON input must be an object or an array of objects"
                )
            return [_verify_record(item) for item in value], False
        if input_format == "nl":
            return [
                _verify_record(json.loads(line))
                for line in stream
                if line.strip()
            ], False
        reader = csv_stdlib.DictReader(
            stream, dialect="excel-tab" if input_format == "tsv" else "excel"
        )
        if reader.fieldnames is None:
            raise click.ClickException("CSV/TSV input must include a header row")
        return [dict(row) for row in reader], False


def _coerce_value(value: Any, python_type: type[Any]) -> Any:
    if value is None or not isinstance(value, str) or python_type is str:
        return value
    if value == "":
        return None
    try:
        if python_type is bool:
            lowered = value.lower()
            if lowered in ("true", "1"):
                return True
            if lowered in ("false", "0"):
                return False
            return value
        if python_type is int:
            return int(value)
        if python_type is float:
            return float(value)
        if python_type is decimal.Decimal:
            return decimal.Decimal(value)
        if python_type in (dict, list):
            return json.loads(value)
        if python_type is datetime.date:
            return datetime.date.fromisoformat(value)
        if python_type is datetime.datetime:
            return datetime.datetime.fromisoformat(value)
        if python_type is datetime.time:
            return datetime.time.fromisoformat(value)
        if python_type is uuid.UUID:
            return uuid.UUID(value)
    except (ValueError, TypeError, json.JSONDecodeError):
        return value
    return value


def _python_type_for_name(type_name: str) -> type[Any]:
    return {
        "BOOLEAN": bool,
        "DATE": datetime.date,
        "DATETIME": datetime.datetime,
        "FLOAT": float,
        "INTEGER": int,
        "JSON": dict,
        "NUMERIC": decimal.Decimal,
        "REAL": float,
        "TIME": datetime.time,
    }.get(type_name.upper(), str)


def _coerce_records(
    records: list[dict[str, Any]],
    reflected_types: Mapping[str, type[Any]],
    explicit_types: Mapping[str, str],
) -> list[dict[str, Any]]:
    types = dict(reflected_types)
    types.update(
        {name: _python_type_for_name(type_name) for name, type_name in explicit_types.items()}
    )
    return [
        {
            name: _coerce_value(value, types.get(name, str))
            for name, value in record.items()
        }
        for record in records
    ]


def _serializable_columns(table: Any) -> list[dict[str, Any]]:
    return [dict(column._asdict()) for column in table.columns]


def _serializable_indexes(table: Any) -> list[dict[str, Any]]:
    return [dict(index._asdict()) for index in table.indexes]


def _serializable_foreign_keys(table: Any) -> list[dict[str, Any]]:
    return [asdict(foreign_key) for foreign_key in table.foreign_keys]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(
    package_name="sqlite-utils-sqlalchemy", prog_name="sqlite-utils-sqlalchemy"
)
def cli() -> None:
    """Use a sqlite-utils-style API with SQLite, PostgreSQL, or DuckDB."""


@cli.command(name="create-table")
@click.argument("database")
@click.argument("table_name")
@click.argument("columns", nargs=-1, required=True)
@click.option("pks", "--pk", multiple=True, help="Primary-key column; repeat for compound keys.")
@click.option("not_null", "--not-null", multiple=True, help="Column to make NOT NULL.")
@click.option("defaults", "--default", multiple=True, type=(str, str), help="Column and JSON default value.")
@click.option("foreign_keys", "--fk", multiple=True, type=(str, str, str), help="Column, other table, and other column.")
@click.option("--ignore", "--if-not-exists", is_flag=True, help="Do nothing if the table exists.")
@click.option("--replace", is_flag=True, help="Drop and recreate an existing table.")
@_translate_errors
def create_table(
    database: str,
    table_name: str,
    columns: tuple[str, ...],
    pks: tuple[str, ...],
    not_null: tuple[str, ...],
    defaults: tuple[tuple[str, str], ...],
    foreign_keys: tuple[tuple[str, str, str], ...],
    ignore: bool,
    replace: bool,
) -> None:
    """Create TABLE using name/type pairs in DATABASE."""
    if ignore and replace:
        raise click.ClickException("Use either --ignore or --replace, not both")
    column_types = _parse_column_pairs(columns)
    pk: str | tuple[str, ...] | None = None
    if len(pks) == 1:
        pk = pks[0]
    elif pks:
        pk = pks
    with _database(database) as db:
        if db[table_name].exists() and not (ignore or replace):
            raise click.ClickException(
                f'Table "{table_name}" already exists. Use --replace to delete and replace it.'
            )
        db[table_name].create(
            column_types,
            pk=pk,
            not_null=not_null,
            defaults=_parse_defaults(defaults),
            foreign_keys=foreign_keys,
            ignore=ignore,
            replace=replace,
        )


def _write_options(function: F) -> F:
    decorators = (
        click.argument("database"),
        click.argument("table_name"),
        click.argument("file"),
        click.option("pks", "--pk", multiple=True, help="Primary-key column; repeat for compound keys."),
        click.option("--nl", is_flag=True, help="Read newline-delimited JSON."),
        click.option("--csv", is_flag=True, help="Read CSV with a header row."),
        click.option("--tsv", is_flag=True, help="Read TSV with a header row."),
        click.option("types", "--type", multiple=True, type=(str, click.Choice(VALID_COLUMN_TYPES, case_sensitive=False)), help="Column and type to use when creating the table."),
        click.option("--alter", is_flag=True, help="Add nullable columns missing from an existing table."),
        click.option("not_null", "--not-null", multiple=True, help="Column to make NOT NULL when creating the table."),
        click.option("defaults", "--default", multiple=True, type=(str, str), help="Column and JSON default value."),
    )
    for decorator in reversed(decorators):
        function = decorator(function)
    return function


def _perform_write(
    *,
    database: str,
    table_name: str,
    file: str,
    pks: tuple[str, ...],
    nl: bool,
    csv: bool,
    tsv: bool,
    types: tuple[tuple[str, str], ...],
    alter: bool,
    not_null: tuple[str, ...],
    defaults: tuple[tuple[str, str], ...],
    upsert: bool,
    ignore: bool = False,
    replace: bool = False,
    truncate: bool = False,
) -> None:
    if ignore and replace:
        raise click.ClickException("Use either --ignore or --replace, not both")
    records, single = _read_records(file, nl=nl, csv=csv, tsv=tsv)
    type_overrides = {name: type_name.upper() for name, type_name in types}
    pk: str | tuple[str, ...] | None = None
    if len(pks) == 1:
        pk = pks[0]
    elif pks:
        pk = pks
    with _database(database) as db:
        table = db[table_name]
        reflected_types = table.columns_dict if table.exists() else {}
        records = _coerce_records(records, reflected_types, type_overrides)
        kwargs = {
            "pk": pk,
            "alter": alter,
            "not_null": not_null,
            "defaults": _parse_defaults(defaults),
            "columns": type_overrides,
        }
        if upsert:
            if single:
                table.upsert(records[0], **kwargs)
            else:
                table.upsert_all(records, **kwargs)
        elif single:
            table.insert(
                records[0], ignore=ignore, replace=replace, truncate=truncate, **kwargs
            )
        else:
            table.insert_all(
                records, ignore=ignore, replace=replace, truncate=truncate, **kwargs
            )


@cli.command()
@_write_options
@click.option("--ignore", is_flag=True, help="Ignore primary-key conflicts.")
@click.option("--replace", is_flag=True, help="Replace rows with primary-key conflicts.")
@click.option("--truncate", is_flag=True, help="Delete existing rows before inserting.")
@_translate_errors
def insert(**kwargs: Any) -> None:
    """Insert one or many records from FILE into TABLE."""
    _perform_write(**kwargs, upsert=False)


@cli.command()
@_write_options
@_translate_errors
def upsert(**kwargs: Any) -> None:
    """Insert records, updating supplied fields on primary-key conflicts."""
    _perform_write(**kwargs, upsert=True)


@cli.command()
@click.argument("database")
@click.argument("table_name")
@click.argument("pk_value")
@click.argument("file")
@click.option("--alter", is_flag=True, help="Add nullable columns missing from the table.")
@_translate_errors
def update(
    database: str, table_name: str, pk_value: str, file: str, alter: bool
) -> None:
    """Apply the JSON object in FILE to one row selected by PK_VALUE."""
    records, single = _read_records(file, nl=False, csv=False, tsv=False)
    if not single:
        raise click.ClickException("update input must be one JSON object")
    with _database(database) as db:
        table = db[table_name]
        updates = _coerce_records(records, table.columns_dict, {})[0]
        table.update(_parse_json_or_string(pk_value), updates, alter=alter)


def _table_listing(
    database: str,
    *,
    views: bool,
    json_output: bool,
    nl: bool,
    counts: bool,
    columns: bool,
    schema: bool,
) -> None:
    with _database(database) as db:
        names = db.view_names() if views else db.table_names()
        key = "view" if views else "table"
        result = []
        for name in names:
            item: dict[str, Any] = {key: name}
            table = db[name]
            if counts:
                item["count"] = table.count
            if columns:
                item["columns"] = [column.name for column in table.columns]
            if schema:
                item["schema"] = table.schema
            result.append(item)
    if not any((json_output, nl, counts, columns, schema)):
        for name in names:
            click.echo(name)
    else:
        _echo_json(result, nl=nl)


def _listing_options(function: F) -> F:
    decorators = (
        click.argument("database"),
        click.option("json_output", "--json", is_flag=True, help="Output a JSON array."),
        click.option("--nl", is_flag=True, help="Output newline-delimited JSON."),
        click.option("--counts", is_flag=True, help="Include a row count."),
        click.option("--columns", is_flag=True, help="Include column names."),
        click.option("--schema", is_flag=True, help="Include reflected DDL."),
    )
    for decorator in reversed(decorators):
        function = decorator(function)
    return function


@cli.command()
@_listing_options
@_translate_errors
def tables(**kwargs: Any) -> None:
    """List tables in DATABASE."""
    _table_listing(**kwargs, views=False)


@cli.command()
@_listing_options
@_translate_errors
def views(**kwargs: Any) -> None:
    """List views in DATABASE."""
    _table_listing(**kwargs, views=True)


@cli.command()
@click.argument("database")
@click.argument("table_names", nargs=-1)
@_translate_errors
def schema(database: str, table_names: tuple[str, ...]) -> None:
    """Show reflected DDL for DATABASE or selected tables."""
    with _database(database) as db:
        value = "\n".join(db[name].schema for name in table_names) if table_names else db.schema
    click.echo(value)


@cli.command()
@click.argument("database")
@click.argument("table_name")
@_translate_errors
def columns(database: str, table_name: str) -> None:
    """Show normalized column metadata for TABLE."""
    with _database(database) as db:
        result = _serializable_columns(db[table_name])
    _echo_json(result)


@cli.command()
@click.argument("database")
@click.argument("table_names", nargs=-1)
@_translate_errors
def indexes(database: str, table_names: tuple[str, ...]) -> None:
    """Show normalized explicit indexes, optionally for selected tables."""
    with _database(database) as db:
        names = table_names or tuple(db.table_names())
        result = []
        for name in names:
            for index in _serializable_indexes(db[name]):
                index.setdefault("table", name)
                result.append(index)
    _echo_json(result)


@cli.command(name="foreign-keys")
@click.argument("database")
@click.argument("table_names", nargs=-1)
@_translate_errors
def foreign_keys(database: str, table_names: tuple[str, ...]) -> None:
    """Show normalized foreign keys, optionally for selected tables."""
    with _database(database) as db:
        names = table_names or tuple(db.table_names())
        result = []
        for name in names:
            result.extend(_serializable_foreign_keys(db[name]))
    _echo_json(result)


@cli.command()
@click.argument("database")
@click.argument("table_name")
@click.option("--nl", is_flag=True, help="Output newline-delimited JSON.")
@_translate_errors
def rows(database: str, table_name: str, nl: bool) -> None:
    """Output every row from TABLE as JSON."""
    with _database(database) as db:
        result = list(db[table_name].rows)
    _echo_json(result, nl=nl)


@cli.command()
@click.argument("database")
@click.argument("table_name")
@click.argument("pk_value")
@_translate_errors
def get(database: str, table_name: str, pk_value: str) -> None:
    """Output one row selected by PK_VALUE as JSON."""
    with _database(database) as db:
        result = db[table_name].get(_parse_json_or_string(pk_value))
    _echo_json(result)


@cli.command()
@click.argument("database")
@click.argument("table_name")
@_translate_errors
def count(database: str, table_name: str) -> None:
    """Output the number of rows in TABLE."""
    with _database(database) as db:
        value = db[table_name].count
    click.echo(value)


if __name__ == "__main__":
    cli()
