from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from click.testing import CliRunner
from sqlalchemy.engine import Engine

import alchemy_utils.cli as cli_module
from alchemy_utils import Database
from alchemy_utils.cli import cli


def invoke(*args: str, input: str | None = None):
    return CliRunner().invoke(cli, list(args), input=input)


def database_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def assert_success(result) -> None:
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_help_lists_the_portable_commands():
    result = invoke("--help")

    assert_success(result)
    for command in (
        "create-table",
        "insert",
        "upsert",
        "update",
        "tables",
        "views",
        "schema",
        "columns",
        "indexes",
        "foreign-keys",
        "rows",
        "get",
    ):
        assert command in result.stdout


def test_version():
    result = invoke("--version")

    assert_success(result)
    assert result.stdout == "alchemy-utils, version 0.1.0\n"


def test_create_insert_upsert_update_and_read(engine: Engine):
    url = database_url(engine)

    result = invoke(
        "create-table",
        url,
        "people",
        "id",
        "integer",
        "name",
        "text",
        "score",
        "integer",
        "--pk",
        "id",
        "--not-null",
        "name",
    )
    assert_success(result)

    result = invoke(
        "insert",
        url,
        "people",
        "-",
        input=json.dumps(
            [
                {"id": 1, "name": "Ada", "score": 10},
                {"id": 2, "name": "Grace", "score": 20},
            ]
        ),
    )
    assert_success(result)
    assert result.stdout == ""

    result = invoke(
        "upsert",
        url,
        "people",
        "-",
        input='{"id": 1, "name": "Ada Lovelace"}',
    )
    assert_success(result)

    result = invoke(
        "update",
        url,
        "people",
        "2",
        "-",
        "--alter",
        input='{"name": "Grace Hopper", "active": true}',
    )
    assert_success(result)

    result = invoke("get", url, "people", "1")
    assert_success(result)
    assert json.loads(result.stdout) == {
        "id": 1,
        "name": "Ada Lovelace",
        "score": 10,
        "active": None,
    }

    result = invoke("rows", url, "people", "--nl")
    assert_success(result)
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {"id": 1, "name": "Ada Lovelace", "score": 10, "active": None},
        {"id": 2, "name": "Grace Hopper", "score": 20, "active": True},
    ]


def test_json_lines_csv_and_tsv_bulk_inputs(engine: Engine, tmp_path: Path):
    url = database_url(engine)

    result = invoke(
        "insert",
        url,
        "events",
        "-",
        "--nl",
        "--pk",
        "id",
        input='{"id": 1, "name": "one"}\n{"id": 2, "name": "two"}\n',
    )
    assert_success(result)

    csv_path = tmp_path / "more events.csv"
    csv_path.write_text("id,name\n3,three\n", encoding="utf-8")
    result = invoke("insert", url, "events", str(csv_path), "--csv")
    assert_success(result)

    tsv_path = tmp_path / "last events.tsv"
    tsv_path.write_text("id\tname\n4\tfour\n", encoding="utf-8")
    result = invoke("upsert", url, "events", str(tsv_path), "--tsv")
    assert_success(result)

    with Database(engine) as db:
        assert list(db["events"].rows) == [
            {"id": 1, "name": "one"},
            {"id": 2, "name": "two"},
            {"id": 3, "name": "three"},
            {"id": 4, "name": "four"},
        ]


def test_create_table_options_and_introspection(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke("create-table", url, "authors", "id", "integer", "--pk", "id")
    )
    assert_success(
        invoke(
            "create-table",
            url,
            "books",
            "id",
            "integer",
            "author_id",
            "integer",
            "title",
            "text",
            "rating",
            "integer",
            "--pk",
            "id",
            "--not-null",
            "title",
            "--default",
            "rating",
            "0",
            "--fk",
            "author_id",
            "authors",
            "id",
        )
    )
    metadata = sa.MetaData()
    books = sa.Table("books", metadata, autoload_with=engine)
    sa.Index("books_title_idx", books.c.title).create(engine)

    result = invoke("tables", url, "--json", "--counts", "--columns")
    assert_success(result)
    tables = {item["table"]: item for item in json.loads(result.stdout)}
    assert set(tables) == {"authors", "books"}
    assert tables["books"]["count"] == 0
    assert tables["books"]["columns"] == ["id", "author_id", "title", "rating"]

    result = invoke("columns", url, "books")
    assert_success(result)
    columns = {item["name"]: item for item in json.loads(result.stdout)}
    assert columns["id"]["is_pk"] == 1
    assert columns["title"]["notnull"] == 1

    result = invoke("foreign-keys", url, "books")
    assert_success(result)
    foreign_keys = json.loads(result.stdout)
    assert foreign_keys[0]["columns"] == ["author_id"]
    assert foreign_keys[0]["other_table"] == "authors"

    result = invoke("indexes", url, "books")
    assert_success(result)
    indexes = {item["name"]: item for item in json.loads(result.stdout)}
    assert indexes["books_title_idx"]["columns"] == ["title"]

    result = invoke("schema", url, "books")
    assert_success(result)
    assert "books" in result.stdout
    assert "FOREIGN KEY" in result.stdout.upper()


def test_compound_keys_nested_json_and_binary_round_trip(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke(
            "create-table",
            url,
            "memberships",
            "organization",
            "text",
            "member_id",
            "integer",
            "profile",
            "json",
            "avatar",
            "blob",
            "--pk",
            "organization",
            "--pk",
            "member_id",
        )
    )
    record = {
        "organization": "acme",
        "member_id": 7,
        "profile": {"roles": ["admin"], "active": True},
        "avatar": {"$base64": True, "encoded": "AP8="},
    }
    assert_success(invoke("insert", url, "memberships", "-", input=json.dumps(record)))
    assert_success(
        invoke(
            "upsert",
            url,
            "memberships",
            "-",
            input='{"organization":"acme","member_id":7,"profile":{"roles":["owner"]}}',
        )
    )

    result = invoke("get", url, "memberships", '["acme", 7]')
    assert_success(result)
    assert json.loads(result.stdout) == {
        "organization": "acme",
        "member_id": 7,
        "profile": {"roles": ["owner"]},
        "avatar": {"$base64": True, "encoded": "AP8="},
    }


def test_insert_conflict_modes_truncate_and_count(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke(
            "insert",
            url,
            "items",
            "-",
            "--pk",
            "id",
            input='{"id": 1, "name": "first", "note": "keep"}',
        )
    )
    assert_success(
        invoke(
            "insert",
            url,
            "items",
            "-",
            "--ignore",
            input='{"id": 1, "name": "ignored"}',
        )
    )
    assert json.loads(invoke("get", url, "items", "1").stdout)["name"] == "first"

    assert_success(
        invoke(
            "insert",
            url,
            "items",
            "-",
            "--replace",
            input='{"id": 1, "name": "replacement"}',
        )
    )
    assert json.loads(invoke("get", url, "items", "1").stdout) == {
        "id": 1,
        "name": "replacement",
        "note": None,
    }

    assert_success(
        invoke(
            "insert",
            url,
            "items",
            "-",
            "--truncate",
            input='[{"id": 2, "name": "only"}]',
        )
    )
    result = invoke("count", url, "items")
    assert_success(result)
    assert result.stdout == "1\n"
    assert json.loads(invoke("rows", url, "items").stdout) == [
        {"id": 2, "name": "only", "note": None}
    ]


def test_bulk_failure_does_not_insert_partial_records(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke(
            "create-table",
            url,
            "numbers",
            "id",
            "integer",
            "--pk",
            "id",
        )
    )
    result = invoke(
        "insert",
        url,
        "numbers",
        "-",
        input='[{"id": 1}, {"id": 1}]',
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert invoke("count", url, "numbers").stdout == "0\n"


def test_default_tables_output_and_views(tmp_path: Path):
    path = tmp_path / "database with spaces.db"
    url = str(path)
    assert_success(invoke("create-table", url, "one", "id", "integer"))
    assert_success(invoke("create-table", url, "two", "id", "integer"))
    with Database(path) as db, db.engine.begin() as connection:
        connection.exec_driver_sql("CREATE VIEW things AS SELECT id FROM one")

    result = invoke("tables", url)
    assert_success(result)
    assert json.loads(result.stdout) == [{"table": "one"}, {"table": "two"}]
    result = invoke("views", url)
    assert_success(result)
    assert json.loads(result.stdout) == [{"view": "things"}]

    result = invoke("views", url, "--plain")
    assert_success(result)
    assert result.stdout == "things\n"


def test_pk_values_use_reflected_types(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke(
            "create-table",
            url,
            "releases",
            "code",
            "text",
            "released",
            "date",
            "name",
            "text",
            "--pk",
            "code",
            "--pk",
            "released",
        )
    )
    assert_success(
        invoke(
            "insert",
            url,
            "releases",
            "-",
            input='{"code":"1","released":"2026-08-12","name":"first"}',
        )
    )

    key = '["1", "2026-08-12"]'
    result = invoke("get", url, "releases", key)
    assert_success(result)
    assert json.loads(result.stdout)["name"] == "first"
    assert_success(
        invoke("update", url, "releases", key, "-", input='{"name":"updated"}')
    )
    assert json.loads(invoke("get", url, "releases", key).stdout)["name"] == "updated"

    assert_success(
        invoke("create-table", url, "labels", "code", "text", "--pk", "code")
    )
    assert_success(invoke("insert", url, "labels", "-", input='{"code":"1"}'))
    result = invoke("get", url, "labels", "1")
    assert_success(result)
    assert json.loads(result.stdout) == {"code": "1"}


def test_nested_base64_shape_is_preserved_inside_json(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke(
            "create-table",
            url,
            "assets",
            "id",
            "integer",
            "metadata",
            "json",
            "content",
            "blob",
            "--pk",
            "id",
        )
    )
    sentinel = {"$base64": True, "encoded": "AP8="}
    record = {"id": 1, "metadata": {"thumbnail": sentinel}, "content": sentinel}
    assert_success(invoke("insert", url, "assets", "-", input=json.dumps(record)))

    result = invoke("get", url, "assets", "1")
    assert_success(result)
    assert json.loads(result.stdout) == record


def test_invalid_typed_values_fail_before_writing(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke(
            "create-table",
            url,
            "measurements",
            "id",
            "integer",
            "amount",
            "integer",
            "--pk",
            "id",
        )
    )
    result = invoke(
        "insert",
        url,
        "measurements",
        "-",
        input='{"id": 1, "amount": "not-an-integer"}',
    )

    assert result.exit_code == 1
    assert "amount" in result.stderr
    assert "not-an-integer" in result.stderr
    assert invoke("count", url, "measurements").stdout == "0\n"

    result = invoke(
        "insert",
        url,
        "measurements",
        "-",
        "--type",
        "amount",
        "text",
        input='{"id": 2, "amount": "2"}',
    )
    assert result.exit_code == 1
    assert "--type" in result.stderr
    assert "existing table" in result.stderr


def test_truncate_and_failed_insert_restores_existing_rows(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke(
            "insert",
            url,
            "numbers",
            "-",
            "--pk",
            "id",
            input='{"id": 1, "name": "original"}',
        )
    )
    result = invoke(
        "insert",
        url,
        "numbers",
        "-",
        "--truncate",
        input='[{"id": 2, "name": "new"}, {"id": 2, "name": "duplicate"}]',
    )

    assert result.exit_code == 1
    assert json.loads(invoke("rows", url, "numbers").stdout) == [
        {"id": 1, "name": "original"}
    ]

    assert_success(invoke("insert", url, "numbers", "-", "--truncate", input="[]"))
    assert invoke("count", url, "numbers").stdout == "0\n"


def test_view_schema_is_view_ddl(engine: Engine):
    url = database_url(engine)
    assert_success(invoke("create-table", url, "source", "id", "integer"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE VIEW visible_source AS SELECT id FROM source")

    result = invoke("views", url, "--schema")
    assert_success(result)
    assert "CREATE VIEW" in json.loads(result.stdout)[0]["schema"].upper()
    result = invoke("schema", url, "visible_source")
    assert_success(result)
    assert "CREATE VIEW" in result.stdout.upper()


def test_compound_foreign_key_from_cli(engine: Engine):
    url = database_url(engine)
    assert_success(
        invoke(
            "create-table",
            url,
            "parents",
            "organization",
            "text",
            "id",
            "integer",
            "--pk",
            "organization",
            "--pk",
            "id",
        )
    )
    assert_success(
        invoke(
            "create-table",
            url,
            "children",
            "organization",
            "text",
            "parent_id",
            "integer",
            "--fk",
            "organization,parent_id",
            "parents",
            "organization,id",
        )
    )

    result = invoke("foreign-keys", url, "children")
    assert_success(result)
    assert json.loads(result.stdout)[0]["columns"] == ["organization", "parent_id"]


def test_missing_duckdb_extra_has_an_install_hint(monkeypatch: pytest.MonkeyPatch):
    def missing_driver(value):
        del value
        raise sa.exc.NoSuchModuleError("Can't load plugin: sqlalchemy.dialects:duckdb")

    monkeypatch.setattr(cli_module, "Database", missing_driver)
    result = invoke("tables", "duckdb:///missing.duckdb")

    assert result.exit_code == 1
    assert "alchemy-utils[duckdb]" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("command", ("columns", "indexes", "foreign-keys"))
def test_explicit_missing_table_introspection_is_an_error(
    command: str, tmp_path: Path
):
    result = invoke(command, str(tmp_path / "missing.db"), "not_there")

    assert result.exit_code == 1
    assert "does not exist" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("args", "message"),
    (
        (("create-table", "db.sqlite", "bad", "id"), "even number"),
        (
            ("create-table", "db.sqlite", "bad", "id", "made-up"),
            "column types",
        ),
        (("insert", "db.sqlite", "bad", "-", "--csv", "--nl"), "only one"),
    ),
)
def test_usage_errors(args: tuple[str, ...], message: str, tmp_path: Path):
    args = tuple(str(tmp_path / arg) if arg == "db.sqlite" else arg for arg in args)
    result = invoke(*args, input="[]")

    assert result.exit_code in (1, 2)
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_python_module_entry_point(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-m", "alchemy_utils", "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "create-table" in result.stdout
