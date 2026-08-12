import pytest
from sqlalchemy import create_engine

from sqlalchemy_utils import Database, NoTable, NotFoundError, PrimaryKeyRequired


@pytest.fixture
def db():
    database = Database(create_engine("sqlite+pysqlite:///:memory:"))
    yield database
    database.close()


def test_database_table_access(db):
    table = db["people"]

    assert table.name == "people"
    assert table.db is db
    assert table.exists() is False
    assert repr(table) == "<Table people (does not exist yet)>"
    assert db.table("people").name == "people"


def test_create_insert_rows_and_get(db):
    people = db["people"].create(
        {"id": int, "name": str, "score": float, "active": bool},
        pk="id",
        not_null={"name"},
    )

    returned = people.insert(
        {"id": 1, "name": "Ada", "score": 9.5, "active": True}
    )

    assert returned is people
    assert people.exists() is True
    assert people.count == 1
    assert list(people.rows) == [
        {"id": 1, "name": "Ada", "score": 9.5, "active": True}
    ]
    assert people.get(1) == {
        "id": 1,
        "name": "Ada",
        "score": 9.5,
        "active": True,
    }
    assert people.last_pk == 1


def test_insert_creates_table_from_record(db):
    table = db["events"].insert(
        {"id": 1, "name": "launched", "metadata": {"source": "test"}},
        pk="id",
    )

    assert table.pks == ["id"]
    assert table.get(1) == {
        "id": 1,
        "name": "launched",
        "metadata": {"source": "test"},
    }


def test_create_existing_table_options(db):
    db["people"].create({"id": int}, pk="id")

    with pytest.raises(Exception):
        db["people"].create({"id": int}, pk="id")

    assert db["people"].create({"id": int}, pk="id", if_not_exists=True)

    db["people"].insert({"id": 1})
    db["people"].create({"slug": str}, pk="slug", replace=True)
    assert db["people"].pks == ["slug"]
    assert db["people"].count == 0


def test_missing_table_and_row_errors(db):
    with pytest.raises(NoTable):
        db["missing"].get(1)

    db["people"].create({"id": int}, pk="id")
    with pytest.raises(NotFoundError):
        db["people"].get(1)


def test_upsert_requires_a_primary_key(db):
    with pytest.raises(PrimaryKeyRequired):
        db["people"].upsert({"name": "Ada"})
