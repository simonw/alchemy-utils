import pytest
from sqlalchemy import text


def test_database_table_and_view_names(db):
    db["people"].create({"id": int, "name": str}, pk="id")
    with db.engine.begin() as connection:
        connection.execute(text("create view people_names as select name from people"))

    assert db.table_names() == ["people"]
    assert db.view_names() == ["people_names"]
    assert [table.name for table in db.tables] == ["people"]
    assert "CREATE TABLE" in db.schema.upper()
    assert "people" in db.schema


def test_columns_primary_keys_and_defaults(db):
    table = db["things"].create(
        {"payload": dict, "part": int, "scope": str, "label": str},
        pk=("scope", "part"),
        column_order=["part", "scope"],
        not_null={"label"},
        defaults={"label": "untitled"},
    )

    assert [column.name for column in table.columns] == [
        "part",
        "scope",
        "payload",
        "label",
    ]
    assert [column.is_pk for column in table.columns] == [2, 1, 0, 0]
    assert (
        next(column for column in table.columns if column.name == "label").notnull == 1
    )
    assert table.pks == ["scope", "part"]
    assert table.columns_dict["payload"] is dict
    assert table.columns_dict["part"] is int
    assert table.default_values["label"] is not None
    assert "PRIMARY KEY" in table.schema.upper()
    assert "scope" in table.schema


def test_foreign_key_introspection(db):
    db["authors"].create({"id": int, "name": str}, pk="id")
    books = db["books"].create(
        {"id": int, "author_id": int, "title": str},
        pk="id",
        foreign_keys=[("author_id", "authors", "id")],
    )

    assert len(books.foreign_keys) == 1
    foreign_key = books.foreign_keys[0]
    assert foreign_key.table == "books"
    assert foreign_key.column == "author_id"
    assert foreign_key.other_table == "authors"
    assert foreign_key.other_column == "id"
    assert foreign_key.columns == ("author_id",)
    assert foreign_key.other_columns == ("id",)
    assert foreign_key.is_compound is False


def test_self_referential_foreign_key(db):
    table = db["nodes"].create(
        {"id": int, "parent_id": int},
        pk="id",
        foreign_keys=(("parent_id", "nodes", "id"),),
    )

    assert table.foreign_keys[0].columns == ("parent_id",)
    assert table.foreign_keys[0].other_table == "nodes"


def test_compound_foreign_key_introspection(db):
    db["parents"].create({"tenant": str, "number": int}, pk=("tenant", "number"))
    children = db["children"].create(
        {"id": int, "tenant": str, "parent_number": int},
        pk="id",
        foreign_keys=[(("tenant", "parent_number"), "parents", ("tenant", "number"))],
    )

    assert len(children.foreign_keys) == 1
    foreign_key = children.foreign_keys[0]
    assert foreign_key.column is None
    assert foreign_key.other_column is None
    assert foreign_key.columns == ("tenant", "parent_number")
    assert foreign_key.other_columns == ("tenant", "number")
    assert foreign_key.is_compound is True


def test_explicit_index_introspection(db):
    table = db["people"].create({"id": int, "name": str}, pk="id")
    with db.engine.begin() as connection:
        connection.execute(
            text('create unique index "ix_people_name" on "people" ("name")')
        )

    assert [(index.name, index.unique, index.columns) for index in table.indexes] == [
        ("ix_people_name", 1, ["name"])
    ]


def test_index_introspection_preserves_commas_in_identifiers(db):
    table = db["people"].create({"id": int, "last,name": str}, pk="id")
    with db.engine.begin() as connection:
        connection.execute(
            text('create index "ix_people_last_name" on "people" ("last,name")')
        )

    assert table.indexes[0].columns == ["last,name"]


def test_partial_index_introspection(db):
    if db.engine.dialect.name == "duckdb":
        pytest.skip("DuckDB does not support partial indexes")
    table = db["people"].create({"id": int, "active": bool}, pk="id")
    with db.engine.begin() as connection:
        connection.execute(
            text(
                'create index "ix_people_active" on "people" ("active") where "active"'
            )
        )

    assert table.indexes[0].partial == 1


def test_no_declared_primary_key_is_not_synthesized(db):
    table = db["logs"].create({"message": str})

    assert table.pks == []
    assert table.use_rowid is (db.engine.dialect.name == "sqlite")
