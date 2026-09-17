"""The boot-time script that ensures the ``svix`` database exists.

It is the first thing ``scripts/start/app.sh`` runs, under ``set -e``, so a failure here means the
API never starts. Production's ``DB_USER`` is a least-privilege role without CREATEDB, and
PostgreSQL checks that privilege before it checks for a duplicate name: the script must read
``pg_database`` before it tries to CREATE, or such a role fails at boot even though the database
was provisioned for it. See scripts/init/create_svix_db.py.

These tests need a superuser on the test cluster (testcontainers and the CI service both give one)
because they create a NOCREATEDB role and, when the cluster has no ``svix`` yet, create and drop
that database. If ``svix`` already exists when the session starts (a ``TEST_DATABASE_URL`` pointed
at a real dev cluster), it is never dropped and the cases that need it absent are skipped.
"""

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import psycopg.errors
import pytest
from psycopg import sql
from pydantic import SecretStr

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "init" / "create_svix_db.py"

SVIX_DB = "svix"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("create_svix_db", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_module()


# ---------------------------------------------------------------------------
# Cluster access
# ---------------------------------------------------------------------------


class _Target:
    """Where the script should connect: the test cluster, as a given role."""

    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or 5432
        self.dbname = parsed.path.lstrip("/")
        self.admin_user = parsed.username or ""
        self.admin_password = parsed.password or ""

    def connect_as_admin(self) -> psycopg.Connection:
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.admin_user,
            password=self.admin_password,
            autocommit=True,
        )


def _svix_exists(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (SVIX_DB,)).fetchone() is not None


@pytest.fixture(scope="session")
def target(_postgres_url: str) -> _Target:
    return _Target(_postgres_url)


@pytest.fixture(scope="session")
def svix_preexisted(target: _Target) -> bool:
    """Whether the cluster already had a ``svix`` database before any test touched it.

    Recorded once per session so that a real database is never dropped by the teardown below.
    """
    with target.connect_as_admin() as conn:
        return _svix_exists(conn)


@pytest.fixture
def admin(target: _Target, svix_preexisted: bool) -> Iterator[psycopg.Connection]:
    """A superuser autocommit connection; drops ``svix`` afterwards only if this session created it."""
    with target.connect_as_admin() as conn:
        yield conn
        if not svix_preexisted and _svix_exists(conn):
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(SVIX_DB)))


@pytest.fixture
def restricted_role(admin: psycopg.Connection) -> Iterator[tuple[str, str]]:
    """A login role without CREATEDB, like production's ``DB_USER``."""
    name = f"ow_test_nocreatedb_{uuid4().hex[:8]}"
    password = uuid4().hex
    admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN NOCREATEDB PASSWORD {}").format(sql.Identifier(name), sql.Literal(password))
    )
    yield name, password
    admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def _svix_present(admin: psycopg.Connection) -> None:
    if not _svix_exists(admin):
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(SVIX_DB)))


def _svix_absent(admin: psycopg.Connection, svix_preexisted: bool) -> None:
    if svix_preexisted:
        pytest.skip("the test cluster already has a 'svix' database; not dropping it")
    if _svix_exists(admin):
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(SVIX_DB)))


def _point_script_at(monkeypatch: pytest.MonkeyPatch, target: _Target, user: str, password: str) -> None:
    monkeypatch.setattr(script.settings, "db_host", target.host)
    monkeypatch.setattr(script.settings, "db_port", target.port)
    monkeypatch.setattr(script.settings, "db_name", target.dbname)
    monkeypatch.setattr(script.settings, "db_user", user)
    monkeypatch.setattr(script.settings, "db_password", SecretStr(password))


# ---------------------------------------------------------------------------
# A role without CREATEDB (production)
# ---------------------------------------------------------------------------


def test_existing_database_is_left_alone_by_a_role_without_createdb(
    admin: psycopg.Connection,
    restricted_role: tuple[str, str],
    target: _Target,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The production boot: ``svix`` was provisioned out of band, DB_USER cannot CREATEDB."""
    _svix_present(admin)
    _point_script_at(monkeypatch, target, *restricted_role)

    script.create_svix_db()

    assert "already exists, skipping" in capsys.readouterr().out
    assert _svix_exists(admin)


def test_missing_database_fails_loud_for_a_role_without_createdb(
    admin: psycopg.Connection,
    restricted_role: tuple[str, str],
    target: _Target,
    svix_preexisted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to skip and no privilege to create: the boot must stop, not carry on without Svix."""
    _svix_absent(admin, svix_preexisted)
    _point_script_at(monkeypatch, target, *restricted_role)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        script.create_svix_db()

    assert not _svix_exists(admin)


# ---------------------------------------------------------------------------
# A role with CREATEDB (docker-compose, dev)
# ---------------------------------------------------------------------------


def test_missing_database_is_created_by_a_role_with_createdb(
    admin: psycopg.Connection,
    target: _Target,
    svix_preexisted: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _svix_absent(admin, svix_preexisted)
    _point_script_at(monkeypatch, target, target.admin_user, target.admin_password)

    script.create_svix_db()

    assert "Created 'svix' database" in capsys.readouterr().out
    assert _svix_exists(admin)


def test_existing_database_is_skipped_by_a_role_with_createdb(
    admin: psycopg.Connection,
    target: _Target,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _svix_present(admin)
    _point_script_at(monkeypatch, target, target.admin_user, target.admin_password)

    script.create_svix_db()

    assert "already exists, skipping" in capsys.readouterr().out
    assert _svix_exists(admin)
