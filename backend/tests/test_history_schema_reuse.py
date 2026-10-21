from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import engine as engine_fixture


def test_history_operational_schema_is_removed_before_reusing_private_database() -> None:
    """Exercise the external-URL fixture lifecycle twice on the same disposable database."""
    with PostgresContainer(image="postgres:18", driver="psycopg") as postgres:
        url = postgres.get_connection_url()
        for _ in range(2):
            lifecycle = engine_fixture.__wrapped__(url)
            test_engine = next(lifecycle)
            with test_engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM wearable_history_sources")) == 0
            lifecycle.close()
            probe = create_engine(url)
            try:
                with probe.connect() as connection:
                    assert (
                        connection.scalar(
                            text(
                                "SELECT count(*) FROM pg_proc WHERE pronamespace='public'::regnamespace "
                                "AND proname LIKE 'wh_%'"
                            )
                        )
                        == 0
                    )
                    assert connection.scalar(text("SELECT to_regclass('public.wearable_history_sources')")) is None
            finally:
                probe.dispose()
