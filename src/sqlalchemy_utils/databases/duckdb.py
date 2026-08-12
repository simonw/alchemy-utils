from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from duckdb_engine import insert

from ..db import Database


class DuckDBDatabase(Database):
    """DuckDB workarounds for gaps in duckdb-engine 0.17 reflection and DDL."""

    def insert_statement(self, table: sa.Table) -> Any:
        return insert(table)

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

    def _native_types(self, table_name: str) -> dict[str, str]:
        with self.engine.connect() as connection:
            return {
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
                        "schema": sa.inspect(self.engine).default_schema_name or "main",
                        "table_name": table_name,
                    },
                )
            }

    def reflect_table(self, table_name: str) -> sa.Table:
        table = super().reflect_table(table_name)
        # duckdb-engine reflects DuckDB's native JSON as VARCHAR, which skips
        # SQLAlchemy's JSON result decoder. Recover the native catalog type.
        native_types = self._native_types(table_name)
        for column in table.columns:
            if native_types.get(column.name) == "JSON":
                column.type = sa.JSON()
        return table

    def introspect_columns(self, table_name: str) -> list[dict[str, Any]]:
        columns = super().introspect_columns(table_name)
        native_types = self._native_types(table_name)
        for column in columns:
            if native_types.get(column["name"]) == "JSON":
                column["type"] = sa.JSON()
        return columns

    def introspect_indexes(self, table_name: str) -> list[dict[str, Any]]:
        query = sa.text(
            """
            select index_name, is_unique, expressions
            from duckdb_indexes()
            where schema_name = :schema and table_name = :table_name
              and not is_primary
            order by index_name
            """
        )
        inspector = sa.inspect(self.engine)
        with self.engine.connect() as connection:
            rows = connection.execute(
                query,
                {
                    "schema": inspector.default_schema_name or "main",
                    "table_name": table_name,
                },
            )
            return [
                {
                    "name": row.index_name,
                    "unique": row.is_unique,
                    "column_names": self._parse_index_expressions(row.expressions),
                    "dialect_options": {},
                }
                for row in rows
            ]

    @staticmethod
    def _parse_index_expressions(expressions: str) -> list[str]:
        # duckdb_indexes() exposes a rendered list rather than a structured
        # array. This handles ordinary column indexes; expression indexes are
        # intentionally returned as their SQL text.
        value = expressions.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        if not value:
            return []
        return [part.strip().strip("'\"") for part in value.split(",")]

    def table_schema(self, table_name: str) -> str:
        query = sa.text(
            """
            select sql from duckdb_tables()
            where schema_name = :schema and table_name = :table_name
            """
        )
        with self.engine.connect() as connection:
            value = connection.scalar(
                query,
                {
                    "schema": sa.inspect(self.engine).default_schema_name or "main",
                    "table_name": table_name,
                },
            )
        return value or super().table_schema(table_name)

    def primary_keys(self, table_name: str) -> list[str]:
        reflected = super().primary_keys(table_name)
        if reflected:
            return reflected

        # duckdb-engine does not currently reflect primary keys. DuckDB's
        # information_schema views preserve compound-key declaration order.
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
        inspector = sa.inspect(self.engine)
        with self.engine.connect() as connection:
            return list(
                connection.scalars(
                    query,
                    {
                        "schema": inspector.default_schema_name or "main",
                        "table_name": table_name,
                    },
                )
            )

    def primary_key_column_options(
        self,
        metadata: sa.MetaData,
        table_name: str,
        column_name: str,
        sql_type: sa.types.TypeEngine[Any],
        pk_count: int,
    ) -> tuple[list[Any], dict[str, Any]]:
        if pk_count == 1 and isinstance(sql_type, sa.Integer):
            sequence = sa.Sequence(f"{table_name}_{column_name}_seq", metadata=metadata)
            return [sequence], {"server_default": sequence.next_value()}
        return [], {"autoincrement": False}

    def drop_table(self, table_name: str) -> None:
        columns = self.introspect_columns(table_name)
        sequence_names = []
        for column in columns:
            default = str(column.get("default") or "")
            if default.startswith("nextval('") and default.endswith("')"):
                sequence_names.append(
                    default.removeprefix("nextval('").removesuffix("')")
                )
        super().drop_table(table_name)
        preparer = self.engine.dialect.identifier_preparer
        with self.engine.begin() as connection:
            for sequence_name in sequence_names:
                connection.exec_driver_sql(
                    f"DROP SEQUENCE IF EXISTS {preparer.quote(sequence_name)}"
                )
