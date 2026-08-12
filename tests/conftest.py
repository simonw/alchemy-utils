from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from testing.postgresql import Postgresql

from sqlalchemy_utils import Database


def _postgres_binary(name: str) -> str | None:
    environment_name = f"{name.upper()}_PATH"
    if os.environ.get(environment_name):
        return os.environ[environment_name]
    found = shutil.which(name)
    if found:
        return found
    pg_config = shutil.which("pg_config")
    if pg_config:
        bindir = subprocess.run(
            [pg_config, "--bindir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        candidate = Path(bindir) / name
        if candidate.exists():
            return str(candidate)
    return None


@pytest.fixture(scope="session")
def postgres_cluster() -> Generator[Postgresql]:
    postgres = _postgres_binary("postgres")
    initdb = _postgres_binary("initdb")
    if not postgres or not initdb:
        pytest.skip("PostgreSQL server binaries are not installed")
    server = Postgresql(postgres=postgres, initdb=initdb)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def postgres_url(postgres_cluster: Postgresql) -> Generator[URL]:
    dsn = postgres_cluster.dsn()
    admin_url = URL.create(
        "postgresql+psycopg",
        username=dsn["user"],
        password=dsn.get("password"),
        host=dsn["host"],
        port=dsn["port"],
        database=dsn["database"],
    )
    database_name = f"test_{uuid.uuid4().hex}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    try:
        yield admin_url.set(database=database_name)
    finally:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture(params=("sqlite", "duckdb", "postgresql"))
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[Engine]:
    if request.param == "sqlite":
        url = URL.create("sqlite+pysqlite", database=str(tmp_path / "test.sqlite"))
    elif request.param == "duckdb":
        url = URL.create("duckdb", database=str(tmp_path / "test.duckdb"))
    else:
        url = request.getfixturevalue("postgres_url")
    value = create_engine(url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def db(engine: Engine) -> Generator[Database]:
    database = Database(engine)
    try:
        yield database
    finally:
        database.close()
