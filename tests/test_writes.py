import pytest
from sqlalchemy.exc import IntegrityError

from sqlite_utils_sqlalchemy import NotFoundError, PrimaryKeyRequired


def test_insert_all_infers_union_of_columns(db):
    records = iter(
        (
            {"id": 1, "name": "one"},
            {"id": 2, "score": 2.5},
        )
    )

    table = db["items"].insert_all(records, pk="id")

    assert table.insert_all([]) is table
    assert table.pks == ["id"]
    assert set(table.columns_dict) == {"id", "name", "score"}
    assert sorted(table.rows, key=lambda row: row["id"]) == [
        {"id": 1, "name": "one", "score": None},
        {"id": 2, "name": None, "score": 2.5},
    ]
    assert table.last_pk is None


def test_empty_insert_all_is_noop(db):
    table = db["items"].insert_all(iter(()), pk="id")

    assert table.exists() is False


def test_single_item_insert_all_sets_last_pk(db):
    table = db["items"].insert_all([{"id": 1, "name": "one"}], pk="id")

    assert table.last_pk == 1


def test_generated_integer_primary_key(db):
    table = db["people"].insert({"name": "Ada"}, pk="id")

    assert table.pks == ["id"]
    assert table.last_pk == 1
    assert table.get(1) == {"id": 1, "name": "Ada"}


def test_insert_conflict_options(db):
    table = db["items"].insert({"id": 1, "name": "one", "note": "keep"}, pk="id")

    with pytest.raises(IntegrityError):
        table.insert({"id": 1, "name": "duplicate"})

    assert table.insert({"id": 1, "name": "ignored"}, ignore=True) is table
    assert table.get(1)["name"] == "one"

    assert table.insert({"id": 1, "name": "replacement"}, replace=True) is table
    assert table.get(1) == {"id": 1, "name": "replacement", "note": None}

    with pytest.raises(ValueError, match="either ignore=True or replace=True"):
        table.insert({"id": 2}, ignore=True, replace=True)


def test_upsert_inserts_and_updates_only_supplied_columns(db):
    table = db["items"].insert({"id": 1, "name": "one", "note": "keep"}, pk="id")

    assert table.upsert({"id": 1, "name": "updated"}) is table
    assert table.get(1) == {"id": 1, "name": "updated", "note": "keep"}
    assert table.last_pk == 1

    table.upsert({"id": 2, "name": "two"})
    assert table.get(2) == {"id": 2, "name": "two", "note": None}


def test_upsert_all_supports_compound_primary_keys(db):
    table = db["inventory"].upsert_all(
        [
            {"shop": "A", "sku": 1, "stock": 3},
            {"shop": "A", "sku": 2, "stock": 4},
        ],
        pk=("shop", "sku"),
    )

    table.upsert_all(
        [
            {"shop": "A", "sku": 1, "stock": 8},
            {"shop": "B", "sku": 1, "stock": 2},
        ]
    )

    assert table.last_pk is None
    assert table.get(("A", 1))["stock"] == 8
    assert table.get(("A", 2))["stock"] == 4
    assert table.get(("B", 1))["stock"] == 2


def test_upsert_validates_primary_key_values(db):
    table = db["items"].create({"id": int, "name": str}, pk="id")

    with pytest.raises(PrimaryKeyRequired):
        table.upsert({"name": "missing id"})
    with pytest.raises(PrimaryKeyRequired):
        table.upsert({"id": None, "name": "null id"})


def test_primary_key_only_upsert_does_nothing_on_conflict(db):
    table = db["items"].upsert({"id": 1}, pk="id")

    assert table.upsert({"id": 1}) is table
    assert list(table.rows) == [{"id": 1}]


def test_duplicate_keys_in_upsert_all_are_applied_in_order(db):
    table = db["items"].upsert_all(
        [
            {"id": 1, "name": "first"},
            {"id": 1, "name": "second"},
            {"id": 1, "name": "last"},
        ],
        pk="id",
    )

    assert table.get(1)["name"] == "last"


def test_insert_all_is_atomic(db):
    table = db["items"].create({"id": int, "name": str}, pk="id")

    with pytest.raises(IntegrityError):
        table.insert_all(
            [
                {"id": 1, "name": "first"},
                {"id": 1, "name": "duplicate"},
            ]
        )

    assert table.count == 0


def test_generated_integer_primary_keys_in_bulk(db):
    table = db["items"].insert_all([{"name": "first"}, {"name": "second"}], pk="id")

    assert sorted(table.rows, key=lambda row: row["id"]) == [
        {"id": 1, "name": "first"},
        {"id": 2, "name": "second"},
    ]
    assert table.last_pk is None


def test_reserved_identifiers_are_quoted(db):
    table = db["select"].insert(
        {"key": 1, "from": "source", "Mixed Case": "value"}, pk="key"
    )

    assert table.get(1) == {
        "key": 1,
        "from": "source",
        "Mixed Case": "value",
    }


def test_update_by_single_and_compound_primary_key(db):
    people = db["people"].insert({"id": 1, "name": "Ada"}, pk="id")

    assert people.update(1, {"name": "Ada Lovelace"}) is people
    assert people.get(1)["name"] == "Ada Lovelace"
    assert people.last_pk == 1
    assert people.update(1, {}) is people

    inventory = db["inventory"].insert(
        {"shop": "A", "sku": 1, "stock": 3}, pk=("shop", "sku")
    )
    inventory.update(("A", 1), {"stock": 7})
    assert inventory.get(("A", 1))["stock"] == 7
    assert inventory.last_pk == ("A", 1)

    with pytest.raises(NotFoundError):
        people.update(99, {"name": "missing"})


def test_alter_adds_columns_for_insert_upsert_and_update(db):
    table = db["items"].insert({"id": 1, "name": "one"}, pk="id")

    table.insert({"id": 2, "name": "two", "rating": 4.5}, alter=True)
    table.upsert({"id": 1, "category": "book"}, alter=True)
    table.update(2, {"available": True}, alter=True)

    assert set(table.columns_dict) == {
        "id",
        "name",
        "rating",
        "category",
        "available",
    }
    assert table.get(1)["category"] == "book"
    assert table.get(2)["available"] is True
