"""FORK (Calibra 2.72.2, design D-04): a retriable wearable-history error holds the Google pull cursor.

BACKLOG ``2.96.9.1-PULL-RETRY-GOOGLE-WITHINGS``: the Google pull caught a retriable history error as
an ordinary failure, so ``sync_vendor_data`` could move the cursor past a span the history gate asked
to see again. Oura was fixed the same way (``test_oura_retriable_history_error_holds_the_pull_cursor``).
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.schemas.sync_status import DataTypeFailure
from app.services.providers.google_health.data_247 import GoogleHealth247Data
from app.services.providers.google_health.metrics import METRICS
from app.services.providers.google_health.workouts import GoogleHealthApiWorkouts
from app.utils.wearable_history_ingress import WearableHistoryMetadataUnavailableError
from tests.providers.google_health.history import open_window  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("open_window")

USER_ID = uuid4()
START = datetime(2026, 10, 20, tzinfo=UTC)
END = datetime(2026, 10, 21, tzinfo=UTC)


class _BoundaryDiag:
    message_primary = "Frozen source rejects pre-start data"


class _BoundaryRejectionError(Exception):
    """A deferred wh_ingress rejection as psycopg raises it at commit."""

    diag = _BoundaryDiag()


def _retriable(kind: str) -> Exception:
    if kind == "metadata":
        return WearableHistoryMetadataUnavailableError("metadata")
    return IntegrityError("COMMIT", {}, _BoundaryRejectionError("commit rejected"))


@pytest.fixture
def data_247() -> GoogleHealth247Data:
    return GoogleHealth247Data(oauth=MagicMock(), connection_repo=MagicMock(), api_base_url="https://health.test")


def _run(
    data_247: GoogleHealth247Data, *, failing: str, error: Exception, sleep_error: Exception | None = None
) -> dict:
    def fake_request(*args: object, **kwargs: object) -> object:
        endpoint = str(kwargs.get("endpoint") or args[6])
        if f"/{failing}/" in endpoint:
            raise error
        return {"dataPoints": []}

    with (
        patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=fake_request),
        patch.object(data_247.sleep, "load_and_save", side_effect=sleep_error, return_value=0),
        patch.object(data_247.settings_repo, "get_data_granularity", return_value=None),
        patch("app.services.providers.google_health.data_247.store_raw_payload"),
    ):
        return data_247.load_and_save_all(MagicMock(), USER_ID, START, END)


@pytest.mark.parametrize("kind", ["metadata", "sql_boundary"])
def test_a_retriable_metric_failure_is_reported_so_the_cursor_holds(data_247: GoogleHealth247Data, kind: str) -> None:
    results = _run(data_247, failing="heart-rate", error=_retriable(kind))

    assert isinstance(results["heart-rate"], DataTypeFailure)


@pytest.mark.parametrize("kind", ["metadata", "sql_boundary"])
def test_a_retriable_sleep_failure_is_reported_so_the_cursor_holds(data_247: GoogleHealth247Data, kind: str) -> None:
    results = _run(data_247, failing="none", error=RuntimeError("unused"), sleep_error=_retriable(kind))

    assert isinstance(results["sleep"], DataTypeFailure)


def test_an_ordinary_failure_keeps_todays_handling(data_247: GoogleHealth247Data) -> None:
    results = _run(data_247, failing="heart-rate", error=RuntimeError("boom"))

    assert "heart-rate" not in results


@pytest.mark.parametrize("kind", ["metadata", "sql_boundary"])
def test_a_retriable_failure_still_counts_when_every_type_fails(data_247: GoogleHealth247Data, kind: str) -> None:
    def fail_all(*args: object, **kwargs: object) -> object:
        raise _retriable(kind)

    with (
        patch("app.services.providers.google_health.data_247.make_authenticated_request", side_effect=fail_all),
        patch.object(data_247.sleep, "load_and_save", side_effect=_retriable(kind)),
        patch.object(data_247.settings_repo, "get_data_granularity", return_value=None),
        patch("app.services.providers.google_health.data_247.store_raw_payload"),
        pytest.raises(RuntimeError, match="All Google 24/7 data types failed"),
    ):
        data_247.load_and_save_all(MagicMock(), USER_ID, START, END)


@pytest.mark.parametrize("kind", ["metadata", "sql_boundary"])
def test_a_retriable_exercise_write_leaves_the_loader(kind: str) -> None:
    workouts = GoogleHealthApiWorkouts(MagicMock(), MagicMock(), MagicMock(), "https://health.test")
    point = {"name": "e1", "exercise": {"interval": {"startTime": "2026-10-20T08:00:00Z"}}}
    with (
        patch.object(workouts, "get_workouts", return_value=[point]),
        patch.object(workouts, "_normalize_workout", return_value=(MagicMock(), MagicMock())),
        patch(
            "app.services.providers.google_health.workouts.event_record_service.create_if_admitted",
            side_effect=_retriable(kind),
        ),
        pytest.raises(WearableHistoryMetadataUnavailableError),
    ):
        workouts.load_data(MagicMock(), USER_ID, start_date=START, end_date=END)


def test_an_ordinary_exercise_failure_is_still_skipped() -> None:
    workouts = GoogleHealthApiWorkouts(MagicMock(), MagicMock(), MagicMock(), "https://health.test")
    point = {"name": "e1"}
    with (
        patch.object(workouts, "get_workouts", return_value=[point]),
        patch.object(workouts, "_normalize_workout", side_effect=ValueError("bad point")),
    ):
        assert workouts.load_data(MagicMock(), USER_ID, start_date=START, end_date=END) == 0


def test_a_boundary_rejection_at_a_metrics_commit_holds_the_cursor_and_the_rest_continue(
    data_247: GoogleHealth247Data,
) -> None:
    """The deferred wh_ingress check raises at commit, after the fetch and the write succeeded."""
    db = MagicMock()
    first = METRICS[0].data_type
    db.commit.side_effect = [_retriable("sql_boundary")] + [None] * 20
    with (
        patch(
            "app.services.providers.google_health.data_247.make_authenticated_request", return_value={"dataPoints": []}
        ),
        patch.object(data_247.sleep, "load_and_save", return_value=0),
        patch.object(data_247.settings_repo, "get_data_granularity", return_value=None),
        patch("app.services.providers.google_health.data_247.store_raw_payload"),
    ):
        results = data_247.load_and_save_all(db, USER_ID, START, END)

    assert isinstance(results[first], DataTypeFailure)
    assert db.commit.call_count == len(METRICS) + 1  # every other type and sleep still committed


def test_a_boundary_rejection_at_the_sleep_commit_holds_the_cursor(data_247: GoogleHealth247Data) -> None:
    db = MagicMock()
    db.commit.side_effect = [None] * len(METRICS) + [_retriable("sql_boundary")]
    with (
        patch(
            "app.services.providers.google_health.data_247.make_authenticated_request", return_value={"dataPoints": []}
        ),
        patch.object(data_247.sleep, "load_and_save", return_value=1),
        patch.object(data_247.settings_repo, "get_data_granularity", return_value=None),
        patch("app.services.providers.google_health.data_247.store_raw_payload"),
    ):
        results = data_247.load_and_save_all(db, USER_ID, START, END)

    assert isinstance(results["sleep"], DataTypeFailure)
