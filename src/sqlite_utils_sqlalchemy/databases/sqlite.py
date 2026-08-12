from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert

from ..db import Database


class SQLiteDatabase(Database):
    """SQLite-specific statement construction."""

    def insert_statement(self, table: sa.Table) -> Any:
        return insert(table)

    def supports_rowid(self) -> bool:
        return True

    def insert_ignore_statement(self, table: sa.Table) -> Any:
        return insert(table).on_conflict_do_nothing()

    def insert_replace_statement(
        self, table: sa.Table, pk_names: list[str], update_names: list[str]
    ) -> Any:
        statement = insert(table)
        return statement.on_conflict_do_update(
            index_elements=pk_names,
            set_={name: statement.excluded[name] for name in update_names},
        )

    def upsert_statement(
        self, table: sa.Table, pk_names: list[str], update_names: list[str]
    ) -> Any:
        statement = insert(table)
        if update_names:
            return statement.on_conflict_do_update(
                index_elements=pk_names,
                set_={name: statement.excluded[name] for name in update_names},
            )
        return statement.on_conflict_do_nothing(index_elements=pk_names)
